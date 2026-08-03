#!/usr/bin/env python3
"""Held-out blinded validation of the REPAIRED hallucination classifier.

The 5-rule repaired classifier (`halluc_validation.classify_adjusted`) was
built from the seed-20260801 blinded sample of 60 names, and its reported
59/60 agreement was measured on that SAME sample — an in-sample fit. This
script produces the held-out answer on a fresh, disjoint sample.

Subcommands (protocol order):

  sample    SEEDED (--seed, default 20260802) stratified sample of 40 distinct
            cited names across arm x RAW-oracle-verdict strata (per arm:
            4 hallucinated, 3 exists, 1 renamed; shortfalls fall back to
            exists), drawn from the post-repair fresh rows
            bench/data/runs/{A..E}/fresh_*.json and EXCLUDING every NAME that
            appears in halluc_blind/blinded_sample.json (the 60 items / 53
            distinct names already audited in-sample). Writes:
              holdout_blind/blinded_sample.json  (NO verdicts; shuffled)
              holdout_blind/sample_key.json      (raw + repaired verdicts;
                                                  SEALED until compare)
            Only aggregate strata counts are printed — no per-item verdicts.

  evidence  Oracle-free pinned-tree evidence per blinded item (git grep at
            61a5e4f338, the rev the agents' file tools saw; cross-check at
            9944fe2973, the bench-lean-fresh typecheck rev), via the SAME
            `evidence_for` used for halluc_blind/evidence.json.
            Writes holdout_blind/evidence.json.

  compare   Requires holdout_blind/truth_labels.json (graded BLIND from
            blinded_sample.json + evidence.json + targeted pinned-tree greps,
            before any verdict is read; protocol order enforced). Joins truth
            with the sealed key and scores BOTH instruments on the held-out
            40: the raw oracle (score_bridge Oracle) and the repaired
            classifier (classify_adjusted, R1-R5). Reports confusion cells,
            collapsed-3-class agreement (real|halluc|noise), binary
            precision/recall for the hallucinated class (strict: a truth
            not_a_citation token flagged positive counts as FP; lenient:
            noise rows excluded), and exact error cases.
            Writes halluc_holdout.json.

Truth vocabulary identical to halluc_validation.py:
  real | renamed | nonexistent | not_a_citation

Deterministic given --seed. No network. Never git-commits.
"""
from __future__ import annotations

import argparse
import json
import random
import sys
from collections import defaultdict
from pathlib import Path

HERE = Path(__file__).resolve().parent          # bench/analysis
sys.path.insert(0, str(HERE))
from halluc_validation import (  # noqa: E402
    ARMS, BLIND_DIR, PIN_AGENT, PIN_TYPECHECK, build_suffix_openable,
    classify_adjusted, classify_all, evidence_for, load_fresh_rows, wilson95)
from score_bridge import Oracle  # noqa: E402  (bench path set by halluc_validation)

HOLD_DIR = HERE / "holdout_blind"
OUT_JSON = HERE / "halluc_holdout.json"

DEFAULT_SEED = 20260802
N_SAMPLE = 40
# per-arm quotas over RAW oracle-verdict strata (sum = 8/arm, 40 total),
# mirroring the original 6:5:1 shape at 2/3 scale; shortfalls fall back to
# exists, exactly as in halluc_validation.cmd_sample.
QUOTA = {"hallucinated": 4, "exists": 3, "renamed": 1}

# collapsed 3-class mapping used for the agreement measure
TRUTH_CLASS = {"real": "real", "renamed": "real",
               "nonexistent": "halluc", "not_a_citation": "noise"}
PRED_CLASS = {"exists": "real", "renamed": "real",
              "hallucinated": "halluc", "noise": "noise"}


def excluded_names() -> set[str]:
    b = json.loads((BLIND_DIR / "blinded_sample.json").read_text())
    return {it["name"] for it in b["items"]}


# --------------------------------------------------------------------------- #
# step 1 — seeded held-out blinded sample                                     #
# --------------------------------------------------------------------------- #
def cmd_sample(args) -> None:
    oracle = Oracle()
    assert oracle.enabled, "oracle sources missing"
    openable = build_suffix_openable(oracle)
    rows = load_fresh_rows()
    citations, _ = classify_all(rows, oracle)
    excl = excluded_names()

    # distinct names per arm x RAW verdict, excluding every already-audited name
    strata: dict[tuple[str, str], list[dict]] = defaultdict(list)
    seen: set[tuple[str, str]] = set()
    for c in citations:
        if c["name"] in excl:
            continue
        key = (c["arm"], c["name"])
        if key in seen:
            continue
        seen.add(key)
        strata[(c["arm"], c["verdict"])].append(c)

    rng = random.Random(args.seed)
    picked: list[dict] = []
    for arm in ARMS:
        arm_quota = dict(QUOTA)
        for verdict in ("hallucinated", "renamed", "exists"):
            pool = sorted(strata.get((arm, verdict), []),
                          key=lambda c: (c["task_id"], c["name"]))
            want = arm_quota[verdict]
            if len(pool) < want:
                arm_quota["exists"] += want - len(pool)
                want = len(pool)
            picked.extend(rng.sample(pool, want) if want else [])
    assert len(picked) == N_SAMPLE, f"sample size {len(picked)} != {N_SAMPLE}"
    assert not {c["name"] for c in picked} & excl, "exclusion leak"

    rng.shuffle(picked)
    HOLD_DIR.mkdir(exist_ok=True)
    blinded = [{"i": i, "arm": c["arm"], "task_id": c["task_id"],
                "name": c["name"], "context": c["context"]}
               for i, c in enumerate(picked)]
    key = []
    for i, c in enumerate(picked):
        lean = rows[c["arm"]][c["task_id"]].get("output_lean")
        adj = classify_adjusted(lean, c["name"], oracle, openable)
        key.append({"i": i, "arm": c["arm"], "task_id": c["task_id"],
                    "name": c["name"], "oracle_verdict": c["verdict"],
                    "repaired_verdict": adj})
    (HOLD_DIR / "blinded_sample.json").write_text(json.dumps(
        {"seed": args.seed, "n": N_SAMPLE, "quota_per_arm": QUOTA,
         "population": "distinct (arm, name) citations, post-repair fresh rows, "
                       "EXCLUDING the 53 distinct names of the seed-20260801 "
                       "in-sample audit (halluc_blind/blinded_sample.json)",
         "items": blinded}, indent=1))
    (HOLD_DIR / "sample_key.json").write_text(json.dumps(
        {"seed": args.seed,
         "SEALED": "do not read before truth_labels.json is complete",
         "items": key}, indent=1))
    print(f"wrote {HOLD_DIR}/blinded_sample.json (+ sealed sample_key.json), "
          f"seed={args.seed}, excluded {len(excl)} audited names")
    counts = defaultdict(int)
    for c in picked:
        counts[c["verdict"]] += 1
    print("raw strata drawn (aggregate only):", dict(counts))


# --------------------------------------------------------------------------- #
# step 2 — oracle-free pinned-tree evidence                                   #
# --------------------------------------------------------------------------- #
def cmd_evidence(_args) -> None:
    blinded = json.loads((HOLD_DIR / "blinded_sample.json").read_text())
    out = []
    for item in blinded["items"]:
        ev = evidence_for(item["name"])
        ev["i"] = item["i"]
        out.append(ev)
        print(f"[{item['i']:2d}] {item['name']}: "
              f"{len([h for h in ev['decl_head_hits'] if not h.startswith('...')])} decl-head, "
              f"{len([h for h in ev['full_name_hits'] if not h.startswith('...')])} full-name hits")
    (HOLD_DIR / "evidence.json").write_text(json.dumps(out, indent=1))
    print(f"wrote {HOLD_DIR}/evidence.json")


# --------------------------------------------------------------------------- #
# step 3 — unblind + score both instruments                                   #
# --------------------------------------------------------------------------- #
def binary_stats(rows: list[dict], pred_field: str, exclude_noise: bool) -> dict:
    """positive = predicted 'hallucinated'; truth-positive = 'nonexistent'.
    strict (exclude_noise=False): a not_a_citation token flagged positive is FP.
    """
    tp = fp = fn = tn = 0
    errors = []
    for r in rows:
        if exclude_noise and r["truth"] == "not_a_citation":
            continue
        opos = r[pred_field] == "hallucinated"
        tpos = r["truth"] == "nonexistent"
        if opos and tpos:
            tp += 1
        elif opos and not tpos:
            fp += 1
            errors.append(r)
        elif not opos and tpos:
            fn += 1
            errors.append(r)
        else:
            tn += 1
    n = tp + fp + fn + tn
    return {"n": n, "tp": tp, "fp": fp, "fn": fn, "tn": tn,
            "hallucinated_precision": round(tp / (tp + fp), 4) if tp + fp else None,
            "hallucinated_precision_wilson95": wilson95(tp, tp + fp) if tp + fp else None,
            "hallucinated_recall": round(tp / (tp + fn), 4) if tp + fn else None,
            "real_precision": round(tn / (tn + fn), 4) if tn + fn else None,
            "real_recall": round(tn / (tn + fp), 4) if tn + fp else None,
            "accuracy": round((tp + tn) / n, 4) if n else None,
            "errors": [{k: e[k] for k in ("i", "name", "arm", "task_id",
                                          "oracle", "repaired", "truth", "note")}
                       for e in errors]}


def cmd_compare(_args) -> None:
    labels_p = HOLD_DIR / "truth_labels.json"
    assert labels_p.exists(), ("truth_labels.json missing — grade the blinded "
                               "sample first (protocol order is enforced)")
    truth = {t["i"]: t for t in json.loads(labels_p.read_text())["items"]}
    key = {k["i"]: k for k in
           json.loads((HOLD_DIR / "sample_key.json").read_text())["items"]}
    assert set(truth) == set(key), "truth labels do not cover the sample"

    rows = []
    for i in sorted(key):
        rows.append({"i": i, "name": key[i]["name"], "arm": key[i]["arm"],
                     "task_id": key[i]["task_id"],
                     "oracle": key[i]["oracle_verdict"],
                     "repaired": key[i]["repaired_verdict"],
                     "truth": truth[i]["truth"],
                     "note": truth[i].get("note", "")})

    truth_counts = defaultdict(int)
    for r in rows:
        truth_counts[r["truth"]] += 1

    def instrument(pred_field: str) -> dict:
        confusion = defaultdict(int)
        agree = 0
        disagreements = []
        for r in rows:
            confusion[f"{pred_field}={r[pred_field]}|truth={r['truth']}"] += 1
            if PRED_CLASS[r[pred_field]] == TRUTH_CLASS[r["truth"]]:
                agree += 1
            else:
                disagreements.append({k: r[k] for k in
                                      ("i", "name", "arm", "task_id", "oracle",
                                       "repaired", "truth", "note")})
        return {"collapsed_agreement": f"{agree}/{len(rows)}",
                "collapsed_agreement_rate": round(agree / len(rows), 4),
                "agreement_wilson95": wilson95(agree, len(rows)),
                "confusion_cells": dict(sorted(confusion.items())),
                "disagreements": disagreements,
                "strict_noise_as_FP": binary_stats(rows, pred_field, False),
                "lenient_noise_excluded": binary_stats(rows, pred_field, True)}

    result = {
        "protocol": "held-out blinded validation; truth labels graded from "
                    "blinded_sample.json + evidence.json + targeted git greps "
                    f"at {PIN_AGENT[:10]} (cross-check {PIN_TYPECHECK[:10]}) "
                    "BEFORE sample_key.json was read",
        "seed": json.loads((HOLD_DIR / "blinded_sample.json").read_text())["seed"],
        "n_sample": len(rows),
        "excluded_names": len(excluded_names()),
        "truth_label_counts": dict(sorted(truth_counts.items())),
        "raw_oracle": instrument("oracle"),
        "repaired_classifier": instrument("repaired"),
        "in_sample_reference": {
            "note": "seed-20260801 sample the rules were DERIVED from "
                    "(halluc_validation.md)",
            "repaired_agreement": "59/60",
            "raw_strict_precision": "4/30 = 13.3%",
            "raw_strict_accuracy": 0.567},
        "all_rows": rows,
    }
    OUT_JSON.write_text(json.dumps(result, indent=1))
    print(f"wrote {OUT_JSON}")
    for inst in ("raw_oracle", "repaired_classifier"):
        r = result[inst]
        s = r["strict_noise_as_FP"]
        print(f"{inst}: agreement {r['collapsed_agreement']} "
              f"| strict precision(halluc)="
              f"{s['tp']}/{s['tp'] + s['fp']}={s['hallucinated_precision']} "
              f"recall={s['hallucinated_recall']} acc={s['accuracy']}")


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sp = sub.add_parser("sample")
    sp.add_argument("--seed", type=int, default=DEFAULT_SEED)
    sub.add_parser("evidence")
    sub.add_parser("compare")
    args = ap.parse_args()
    {"sample": cmd_sample, "evidence": cmd_evidence,
     "compare": cmd_compare}[args.cmd](args)
    return 0


if __name__ == "__main__":
    sys.exit(main())
