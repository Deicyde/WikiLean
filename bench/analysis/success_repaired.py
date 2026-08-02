#!/usr/bin/env python3
"""Contamination chase — Tier-1 grounded-typecheck success under the REPAIRED
hallucination oracle (v3 candidate; bridge_summary_v2.json stays untouched).

Context (bench/analysis/halluc_validation.{md,json}): the raw oracle's
"hallucinated" class had 13.3% blinded precision — most flags are
namespace-short-name (R5) and dot-notation/comment/import/self-decl artifacts
(R1-R4); the 5-rule mechanically repaired oracle reaches 59/60 agreement with
blinded ground truth. But folded success (bench/score_bridge.py) uses the RAW
oracle as a hard conjunct (len(hallucinated)==0), and false flags hit C/E far
harder than D (fresh affected rows: D 17 vs C 38 vs E 39) — the primary metric
is biased in D's favor by instrument error.

What this script does (deterministic apart from the REPL rig, which is pinned):

  1. Reuses classify_adjusted (the 5 repair rules) VERBATIM from
     halluc_validation.py, and Oracle/extract_cited from score_bridge.py.
  2. Identifies fresh rows whose folded outcome could change:
     produced ∧ no runner error ∧ raw-halluc>0 ∧ repaired-halluc==0.
     (Repair only clears flags, so success can only increase; a raw-clean
     row's outcome is instrument-invariant.)  Cross-checked per arm against
     halluc_validation.json run_level minus adjusted_oracle_sensitivity.
  3. Typechecks those rows' output_lean through the IDENTICAL pipeline that
     produced bridge_summary_v2.json: score_bridge.score_run -> typecheck_stub
     -> fresh-pin REPL server (bench-lean-fresh, toolchain v4.33.0-rc1,
     mathlib_rev 9944fe2973), task={} i.e. empty gold_header, exactly as
     score_e31_v2.py / the Part-1 pass graded fresh rows.  Rig identity is
     gated up front (ping) like score_e31_v2.py.
  4. Fresh-100 grounded-typecheck table under BOTH instruments (raw = the
     bridge_summary_v2 numbers, re-derived from its paired_matrix and asserted
     against part1_fresh100_v2.json; repaired = new), Wilson 95% CIs;
     paired exact McNemars D-vs-E/C/A, C-vs-A, E-vs-A under both instruments;
     commit-clustered paired bootstrap (fresh_clustered.py's cluster_boot_rd
     imported, same 44 commit clusters from fresh_tasks.jsonl, same base seed
     + per-job seed offsets) for D-vs-E, D-vs-C, D-vs-A — the RAW bootstrap is
     recomputed with the same seeds and asserted equal to fresh_clustered.json,
     proving the clustering replication is byte-identical before the repaired
     numbers are read off.
  5. EVAL-341: the affected-row count per arm is computed the same way
     (280 rows total > the ~150-row typecheck budget), so the eval table is
     NOT recomputed — the counts are recorded as a known limitation.

Requires the fresh typecheck server:
  python3 bench/typecheck.py --server \
    --project /Users/jack/Desktop/LEAN/bench-lean-fresh \
    --socket /tmp/wikilean_tc_fresh.sock \
    --repl-bin /Users/jack/Desktop/LEAN/lean-repl-fresh/.lake/build/bin/repl

Run:  python3 bench/analysis/success_repaired.py
Outputs: success_repaired.json, success_repaired.md (this directory).
Incremental typecheck results are cached (scratchpad or $SUCCESS_REPAIRED_CACHE)
so an interrupted run resumes without re-paying completed checks.
"""
from __future__ import annotations

import json
import os
import socket
import sys
from collections import defaultdict
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent          # bench/analysis
BENCH = HERE.parent                              # bench/
sys.path.insert(0, str(BENCH))
sys.path.insert(0, str(HERE))
import score_bridge  # noqa: E402
from halluc_validation import (build_suffix_openable, classify_adjusted,  # noqa: E402
                               load_fresh_rows)
from fresh_clustered import (B, JOB_SEEDS, PAIRS, SEED,  # noqa: E402
                             cluster_boot_rd, mcnemar_exact, paired_rd_wald,
                             wilson_ci)

RUNS = BENCH / "data" / "runs"
ARMS = ["A", "B", "C", "D", "E"]
SUMMARY_V2 = HERE / "bridge_summary_v2.json"
PART1_V2 = HERE / "part1_fresh100_v2.json"
HALLUC_VAL = HERE / "halluc_validation.json"
FRESH_CLUSTERED = HERE / "fresh_clustered.json"
TASKS_FILE = BENCH / "data" / "fresh_tasks.jsonl"
OUT_JSON = HERE / "success_repaired.json"
OUT_MD = HERE / "success_repaired.md"

FRESH_SOCK = "/tmp/wikilean_tc_fresh.sock"
EXPECT_TOOLCHAIN = "leanprover/lean4:v4.33.0-rc1"
EXPECT_MATHLIB_PREFIX = "9944fe2973"
EVAL_BUDGET = 150      # max typecheck load for the eval table (protocol)
ALPHA = 0.05

CACHE = Path(os.environ.get(
    "SUCCESS_REPAIRED_CACHE",
    "/private/tmp/claude-501/-Users-jack-Desktop-LEAN-WikiLean/"
    "0b16d2c8-53d8-49d0-8e6e-c03de5fb2eff/scratchpad/success_repaired_tc_cache.json"))

MCN_PAIRS = [("D", "E"), ("D", "C"), ("D", "A"), ("C", "A"), ("E", "A")]

# ---------------------------------------------------------------------------
# Server-only typecheck routing. typecheck.typecheck silently falls back to
# SINGLE-SHOT when the client-side socket timeout (~210s) fires — which happens
# whenever the server is re-importing Mathlib after a repl recycle. Single-shot
# on rows that import Mathlib wholesale then records rig-artifact timeout
# verdicts AND competes with the re-import for memory. Patch the seam so every
# check goes through the persistent server, retrying through re-imports; the
# pin is asserted on every returned verdict.
# ---------------------------------------------------------------------------
import time as _time  # noqa: E402
import typecheck as _tcmod  # noqa: E402


def _server_only_typecheck(code, env, *, timeout, max_workers, wait_timeout):
    deadline = _time.time() + 7200
    attempt = 0
    while True:
        r = _tcmod._server_typecheck(code, env, timeout=timeout)
        if r is not None:
            assert r.get("toolchain") == EXPECT_TOOLCHAIN, r
            assert str(r.get("mathlib_rev", "")).startswith(EXPECT_MATHLIB_PREFIX), r
            return r
        attempt += 1
        if _time.time() > deadline:
            raise SystemExit("fresh typecheck server unreachable for 2h — "
                             "aborting (single-shot fallback is disallowed)")
        print(f"      server busy (re-import?) — attempt {attempt}, "
              f"waiting 30s", flush=True)
        _time.sleep(30)


_tcmod.typecheck = _server_only_typecheck   # score_bridge.typecheck_stub uses this


def ping_server(sock_path: str) -> dict:
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
        s.settimeout(30.0)
        s.connect(sock_path)
        s.sendall(json.dumps({"ping": True}).encode())
        s.shutdown(socket.SHUT_WR)
        chunks = []
        while True:
            b = s.recv(65536)
            if not b:
                break
            chunks.append(b)
    return json.loads(b"".join(chunks).decode())


def classify_row(lean: str | None, oracle, openable) -> tuple[list[str], list[str]]:
    """(raw hallucinated names, repaired hallucinated names) for one row."""
    names = score_bridge.extract_cited(lean)
    raw = [n for n in names if oracle.classify(n) == "hallucinated"]
    rep = [n for n in names
           if classify_adjusted(lean, n, oracle, openable) == "hallucinated"]
    return raw, rep


def mcn_block(succ: dict[str, dict[str, bool]], ids: list[str],
              x: str, y: str) -> dict:
    b = sum(1 for t in ids if succ[t][x] and not succ[t][y])
    c = sum(1 for t in ids if succ[t][y] and not succ[t][x])
    both = sum(1 for t in ids if succ[t][x] and succ[t][y])
    n = len(ids)
    wald = paired_rd_wald(b, c, n)
    return {"pair": f"{x}_vs_{y}", "n_paired": n, "both_success": both,
            f"{x}_only": b, f"{y}_only": c, "neither": n - both - b - c,
            "discordant": b + c,
            "p_exact_binomial_two_sided": round(mcnemar_exact(b, c), 6),
            "rd": round(wald["rd"], 6),
            "rd_ci95_paired_wald": [round(v, 6) for v in wald["ci95"]]}


def main() -> int:
    # ---- rig identity gate (same as score_e31_v2.py) -----------------------
    if not Path(FRESH_SOCK).exists():
        sys.exit(f"fresh typecheck server not up at {FRESH_SOCK} — see docstring")
    pong = ping_server(FRESH_SOCK)
    assert pong.get("toolchain") == EXPECT_TOOLCHAIN, pong
    assert str(pong.get("mathlib_rev", "")).startswith(EXPECT_MATHLIB_PREFIX), pong
    print(f"fresh rig OK: {pong['toolchain']} mathlib={pong['mathlib_rev'][:12]}")

    oracle = score_bridge.Oracle(enabled=True)
    assert oracle.enabled, "decl oracle failed to load"
    openable = build_suffix_openable(oracle)
    tasks = score_bridge.load_tasks(score_bridge.DEFAULT_TASKS)  # fresh -> {}

    v2 = json.loads(SUMMARY_V2.read_text())
    pm = v2["paired_matrix"]
    fresh_ids = sorted(t for t in pm if t.startswith("fresh_"))
    assert len(fresh_ids) == 100

    # ---- raw outcomes (instrument = raw oracle), asserted vs part1 ---------
    raw_succ = {t: {a: bool(pm[t][a]) for a in ARMS} for t in fresh_ids}
    part1 = json.loads(PART1_V2.read_text())
    for a in ARMS:
        k = sum(raw_succ[t][a] for t in fresh_ids)
        assert k == part1["fresh_100_table_v2"][a]["k"], (a, k)
    print("raw outcome cross-check OK: per-arm k matches part1_fresh100_v2.json")

    # ---- affected fresh rows (could flip under the repaired instrument) ----
    fresh_rows = load_fresh_rows()          # arm -> tid -> row (asserts 100/arm)
    affected: dict[str, list[str]] = {a: [] for a in ARMS}
    for arm in ARMS:
        for tid in sorted(fresh_rows[arm]):
            row = fresh_rows[arm][tid]
            if not row.get("output_lean") or row.get("error"):
                continue
            raw_h, rep_h = classify_row(row["output_lean"], oracle, openable)
            if raw_h and not rep_h:
                assert not raw_succ[tid][arm], (arm, tid)   # raw halluc => raw fail
                affected[arm].append(tid)

    # cross-check per arm against halluc_validation.json (raw minus repaired
    # any-halluc run counts must equal the affected counts)
    hv = json.loads(HALLUC_VAL.read_text())
    for a in ARMS:
        expect = (hv["run_level"]["per_arm"][a]["runs_any_halluc"]
                  - hv["adjusted_oracle_sensitivity"]["per_arm"][a]["runs_any_halluc"])
        assert len(affected[a]) == expect, (a, len(affected[a]), expect)
    n_aff = sum(len(v) for v in affected.values())
    print(f"affected fresh rows (raw-flagged, repaired-clean): "
          f"{ {a: len(affected[a]) for a in ARMS} } total {n_aff} "
          f"(cross-checked vs halluc_validation.json)")

    # ---- typecheck the affected rows (identical Part-1 pipeline) -----------
    cache: dict[str, dict] = {}
    if CACHE.exists():
        cache = json.loads(CACHE.read_text())
    if OUT_JSON.exists():   # self-seed from a previous complete/partial run
        try:
            prev = json.loads(OUT_JSON.read_text())
            for r in prev.get("affected_rows_detail", []):
                cache.setdefault(f"{r['arm']}:{r['task_id']}", r["typecheck_result"])
        except (json.JSONDecodeError, KeyError):
            pass

    detail: list[dict] = []
    done = 0
    for arm in ARMS:
        for tid in affected[arm]:
            key = f"{arm}:{tid}"
            done += 1
            if key in cache:
                tcr = cache[key]
                print(f"  [{done:3d}/{n_aff}] {arm}/{tid}: cached tc={tcr['ok']}")
            else:
                row = fresh_rows[arm][tid]
                # Rig-fault protection: a repl death mid-check (memory pressure)
                # surfaces as a "repl error"/"server could not import" result —
                # an instrument failure, NOT a typecheck verdict. Retry those
                # (the server re-imports Mathlib into a pristine env); genuine
                # Lean errors and 90s timeouts are verdicts, kept as in Part-1.
                rig_fault = False
                for attempt in range(3):
                    s = score_bridge.score_run(tasks.get(tid, {}), row, oracle)
                    assert s["typecheck"] is not None, (arm, tid)
                    errs = (row.get("_typecheck") or {}).get("errors") or []
                    rig_fault = (s["typecheck"] is False
                                 and not s["typecheck_timed_out"]
                                 and any("repl error" in e
                                         or "server could not import" in e
                                         for e in errs))
                    if not rig_fault:
                        break
                    print(f"      rig fault on {arm}/{tid} "
                          f"(attempt {attempt + 1}/3) — retrying", flush=True)
                if rig_fault:
                    print(f"      WARNING {arm}/{tid}: rig fault persisted; "
                          f"recorded as tc fail (rig_fault_persisted)", flush=True)
                tcr = {"ok": bool(s["typecheck"]),
                       "timed_out": bool(s["typecheck_timed_out"]),
                       "elapsed_s": (row.get("_typecheck") or {}).get("elapsed_s"),
                       "errors": (row.get("_typecheck") or {}).get("errors") or []}
                if rig_fault:
                    tcr["rig_fault_persisted"] = True
                if tcr["ok"]:
                    tcr["errors"] = []
                cache[key] = tcr
                CACHE.parent.mkdir(parents=True, exist_ok=True)
                CACHE.write_text(json.dumps(cache, indent=1))
                print(f"  [{done:3d}/{n_aff}] {arm}/{tid}: tc={tcr['ok']} "
                      f"({tcr['elapsed_s']}s)")
            raw_h, rep_h = classify_row(fresh_rows[arm][tid]["output_lean"],
                                        oracle, openable)
            detail.append({
                "arm": arm, "task_id": tid,
                "raw_hallucinated_names": raw_h,
                "repaired_hallucinated_names": rep_h,
                "raw_folded_success": False,
                "repaired_folded_success": tcr["ok"],
                "typecheck_result": tcr,
            })

    # ---- repaired outcomes -------------------------------------------------
    rep_succ = {t: dict(raw_succ[t]) for t in fresh_ids}
    for r in detail:
        rep_succ[r["task_id"]][r["arm"]] = r["repaired_folded_success"]
    for t in fresh_ids:            # repair can only clear flags -> monotone
        for a in ARMS:
            assert rep_succ[t][a] >= raw_succ[t][a], (t, a)

    def table(succ):
        out = {}
        for a in ARMS:
            k = sum(succ[t][a] for t in fresh_ids)
            lo, hi = wilson_ci(k, 100)
            out[a] = {"k": k, "n": 100, "rate": round(k / 100, 4),
                      "wilson95": [round(lo, 4), round(hi, 4)]}
        return out

    tab_raw = table(raw_succ)
    tab_rep = table(rep_succ)

    tc_pass = {a: {"affected": len(affected[a]),
                   "tc_pass": sum(1 for r in detail
                                  if r["arm"] == a and r["repaired_folded_success"]),
                   } for a in ARMS}
    for a in ARMS:
        n_a = tc_pass[a]["affected"]
        tc_pass[a]["tc_pass_rate"] = (round(tc_pass[a]["tc_pass"] / n_a, 4)
                                      if n_a else None)

    # ---- McNemars under both instruments -----------------------------------
    mcn_raw = {f"{x}_vs_{y}": mcn_block(raw_succ, fresh_ids, x, y)
               for x, y in MCN_PAIRS}
    mcn_rep = {f"{x}_vs_{y}": mcn_block(rep_succ, fresh_ids, x, y)
               for x, y in MCN_PAIRS}
    # consistency with published raw numbers where they exist
    for p in ("D_vs_E", "D_vs_C", "D_vs_A", "E_vs_A"):
        pub = part1["mcnemar_fresh_100_v2"].get(p)
        if pub:
            x, y = p.split("_vs_")
            assert (mcn_raw[p][f"{x}_only"], mcn_raw[p][f"{y}_only"]) == \
                (pub[f"{x}_only"], pub[f"{y}_only"]), p

    flips = {}
    for p in mcn_raw:
        sr = mcn_raw[p]["p_exact_binomial_two_sided"] < ALPHA
        sp = mcn_rep[p]["p_exact_binomial_two_sided"] < ALPHA
        flips[p] = {"raw_significant": sr, "repaired_significant": sp,
                    "classification_changed": sr != sp}

    # ---- commit-clustered paired bootstrap (replicating fresh_clustered) ---
    tasks_meta = [json.loads(l) for l in TASKS_FILE.read_text().splitlines() if l]
    assert len(tasks_meta) == 100
    commit_of = {t["id"]: t["added_in"]["commit"] for t in tasks_meta}
    assert set(commit_of) == set(fresh_ids)
    n_commits = len(set(commit_of.values()))

    def diffs_by_commit(succ, x, y):
        by = defaultdict(list)
        for tid in sorted(succ):
            by[commit_of[tid]].append(int(succ[tid][x]) - int(succ[tid][y]))
        return [np.array(by[g], dtype=float) for g in sorted(by)]

    fc = json.loads(FRESH_CLUSTERED.read_text())
    boot_raw, boot_rep = {}, {}
    for x, y in PAIRS:                       # [("D","E"),("D","C"),("D","A")]
        seed = SEED + JOB_SEEDS[(x, y, "commit")]
        br = cluster_boot_rd(diffs_by_commit(raw_succ, x, y), seed=seed)
        published = fc["clustered_bootstraps"][f"{x}_vs_{y}"]["commit"]
        assert br == published, (f"{x}_vs_{y}", br, published)
        boot_raw[f"{x}_vs_{y}"] = br
        boot_rep[f"{x}_vs_{y}"] = cluster_boot_rd(
            diffs_by_commit(rep_succ, x, y), seed=seed)
    print("clustered-bootstrap replication check OK: raw results are "
          "byte-identical to fresh_clustered.json (same clusters, seeds, B)")

    boot_flips = {}
    for p in boot_raw:
        sr = boot_raw[p]["p_two_sided_percentile_inversion"] < ALPHA
        sp = boot_rep[p]["p_two_sided_percentile_inversion"] < ALPHA
        boot_flips[p] = {"raw_significant": sr, "repaired_significant": sp,
                         "classification_changed": sr != sp}

    # ---- EVAL-341 (+dev) affected counts — typecheck load over budget ------
    bt = {}
    for line in (BENCH / "data" / "bridge_tasks.jsonl").read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        r = json.loads(line)
        if "_meta" not in r:
            bt[r["id"]] = r
    eval_ids = sorted(t for t in bt if bt[t]["split"] == "eval")
    dev_ids = sorted(t for t in bt if bt[t]["split"] == "dev")
    assert len(eval_ids) == 341 and len(dev_ids) == 30

    eval_aff = {a: [] for a in ARMS}
    dev_aff = {a: [] for a in ARMS}
    for arm in ARMS:
        for f in sorted((RUNS / arm).glob("*.json")):
            if ".judge" in f.name:
                continue
            row = json.loads(f.read_text())
            tid = row.get("task_id") or f.stem
            if tid.startswith("fresh_"):
                continue
            if not row.get("output_lean") or row.get("error"):
                continue
            raw_h, rep_h = classify_row(row["output_lean"], oracle, openable)
            if raw_h and not rep_h:
                assert pm[tid][arm] is not True, (tid, arm)
                (eval_aff if tid in bt and bt[tid]["split"] == "eval"
                 else dev_aff)[arm].append(tid)
    n_eval_aff = sum(len(v) for v in eval_aff.values())
    eval_raw_table = {}
    for a in ARMS:
        k = sum(1 for t in eval_ids if pm[t][a] is True)
        lo, hi = wilson_ci(k, len(eval_ids))
        eval_raw_table[a] = {"k": k, "n": len(eval_ids),
                             "rate": round(k / len(eval_ids), 4),
                             "wilson95": [round(lo, 4), round(hi, 4)]}
    print(f"eval affected rows: { {a: len(eval_aff[a]) for a in ARMS} } "
          f"total {n_eval_aff} (> budget {EVAL_BUDGET} -> fresh-only typecheck; "
          f"disclosed as limitation)")

    # ---- assemble ----------------------------------------------------------
    out = {
        "generated_by": "bench/analysis/success_repaired.py",
        "what": ("Fresh-100 grounded-typecheck success recomputed under the "
                 "5-rule REPAIRED hallucination oracle (halluc_validation.py "
                 "classify_adjusted); v3 candidate — bridge_summary_v2.json "
                 "untouched"),
        "success_metric_raw": v2["success_metric"],
        "success_metric_repaired": ("produced ∧ no-halluc(REPAIRED oracle: R1-R4 "
                                    "token drops + R5 namespace reclassification) "
                                    "∧ TYPECHECK (judge pending calibration)"),
        "grading_env": {
            "project": "/Users/jack/Desktop/LEAN/bench-lean-fresh",
            "socket": FRESH_SOCK,
            "toolchain": pong["toolchain"],
            "mathlib_rev": pong["mathlib_rev"],
            "pipeline": ("score_bridge.score_run -> typecheck_stub -> fresh-pin "
                         "REPL server; task={} (empty gold_header) — identical "
                         "to score_e31_v2.py / the Part-1 fresh scoring"),
        },
        "affected_fresh_rows_per_arm": tc_pass,
        "affected_fresh_total": n_aff,
        "fresh_100_table_both_instruments": {
            "raw_oracle": tab_raw, "repaired_oracle": tab_rep},
        "mcnemar_fresh_100_raw": mcn_raw,
        "mcnemar_fresh_100_repaired": mcn_rep,
        "mcnemar_classification_changes_alpha05": flips,
        "commit_clustered_bootstrap": {
            "method": ("fresh_clustered.py cluster_boot_rd, 44 commit clusters "
                       f"from fresh_tasks.jsonl added_in.commit, B={B}, base "
                       f"seed {SEED} + frozen per-job offsets (same seeds both "
                       "instruments); raw replication asserted byte-identical "
                       "to fresh_clustered.json"),
            "n_clusters": n_commits,
            "raw_oracle": boot_raw, "repaired_oracle": boot_rep,
            "classification_changes_alpha05": boot_flips},
        "eval_341_limitation": {
            "affected_rows_per_arm": {a: len(eval_aff[a]) for a in ARMS},
            "affected_total": n_eval_aff,
            "typecheck_budget": EVAL_BUDGET,
            "decision": ("affected typecheck load exceeds the budget; the "
                         "EVAL-341 table is NOT recomputed under the repaired "
                         "instrument — disclose these per-arm affected counts "
                         "(upper bounds on per-arm success gains) as a known "
                         "limitation in the report"),
            "eval_341_raw_table_for_context": eval_raw_table,
            "affected_task_ids": {a: eval_aff[a] for a in ARMS},
        },
        "dev_30_affected_per_arm_for_the_record": {a: len(dev_aff[a]) for a in ARMS},
        "affected_rows_detail": detail,
    }
    OUT_JSON.write_text(json.dumps(out, indent=1) + "\n")
    print(f"wrote {OUT_JSON}")

    # ---- markdown ----------------------------------------------------------
    L: list[str] = []
    A_ = L.append
    A_("# Fresh-100 success under the repaired hallucination oracle (v3 candidate)")
    A_("")
    A_("Generated by `bench/analysis/success_repaired.py`. The blinded validation")
    A_("(`halluc_validation.md`) showed the raw oracle's *hallucinated* class has")
    A_("13.3% precision, and `score_bridge.py` folds that raw flag into success as a")
    A_("hard conjunct — with false flags hitting C/E far harder than D. This file")
    A_("recomputes Tier-1 grounded-typecheck success with the 5-rule mechanically")
    A_("repaired oracle (59/60 blinded agreement) and quantifies how every headline")
    A_("contrast moves. Raw numbers are re-derived from `bridge_summary_v2.json` and")
    A_("asserted against `part1_fresh100_v2.json` / `fresh_clustered.json`;")
    A_("`bridge_summary_v2.json` itself is untouched.")
    A_("")
    A_(f"Typecheck rig: `{pong['toolchain']}`, mathlib `{pong['mathlib_rev'][:12]}`")
    A_("(the identical fresh-pin REPL pipeline that graded the Part-1 rows).")
    A_("")
    A_("## Affected rows (raw-flagged, repaired-clean; only these can flip)")
    A_("")
    A_("| arm | affected (fresh) | typecheck pass | pass rate | affected (eval-341) |")
    A_("|---|---|---|---|---|")
    for a in ARMS:
        tp = tc_pass[a]
        rate = f"{tp['tc_pass_rate']:.0%}" if tp["tc_pass_rate"] is not None else "—"
        A_(f"| {a} | {tp['affected']} | {tp['tc_pass']} | {rate} "
           f"| {len(eval_aff[a])} |")
    A_(f"| **total** | **{n_aff}** | "
       f"**{sum(tc_pass[a]['tc_pass'] for a in ARMS)}** | | **{n_eval_aff}** |")
    A_("")
    A_("Success is monotone under the repair (flags are only ever cleared), so the")
    A_("repaired table dominates the raw table pointwise.")
    A_("")
    A_("## Fresh-100 grounded-typecheck, BOTH instruments")
    A_("")
    A_("| arm | raw k/n | raw rate [Wilson 95] | repaired k/n | repaired rate [Wilson 95] | Δ |")
    A_("|---|---|---|---|---|---|")
    for a in ARMS:
        r, p = tab_raw[a], tab_rep[a]
        A_(f"| {a} | {r['k']}/100 | {r['rate']:.2f} [{r['wilson95'][0]:.3f}, "
           f"{r['wilson95'][1]:.3f}] | {p['k']}/100 | {p['rate']:.2f} "
           f"[{p['wilson95'][0]:.3f}, {p['wilson95'][1]:.3f}] | +{p['k'] - r['k']} |")
    A_("")
    A_("## Paired McNemar (exact binomial two-sided), both instruments")
    A_("")
    A_("| pair | raw b/c | raw p | repaired b/c | repaired p | classification |")
    A_("|---|---|---|---|---|---|")
    for x, y in MCN_PAIRS:
        p = f"{x}_vs_{y}"
        mr, mp = mcn_raw[p], mcn_rep[p]
        fl = flips[p]
        lab = ("**CHANGED**: " if fl["classification_changed"] else "unchanged: ")
        lab += (("sig" if fl["raw_significant"] else "ns") + " → "
                + ("sig" if fl["repaired_significant"] else "ns"))
        A_(f"| {x} vs {y} | {mr[f'{x}_only']}/{mr[f'{y}_only']} "
           f"| {mr['p_exact_binomial_two_sided']:.4g} "
           f"| {mp[f'{x}_only']}/{mp[f'{y}_only']} "
           f"| {mp['p_exact_binomial_two_sided']:.4g} | {lab} |")
    A_("")
    A_("## Commit-clustered paired bootstrap (44 clusters, B=10,000, same seeds)")
    A_("")
    A_("Raw column asserted byte-identical to `fresh_clustered.json` before the")
    A_("repaired column was computed (same clusters, same per-job seeds).")
    A_("")
    A_("| pair | raw RD [95% CI] p | repaired RD [95% CI] p | classification |")
    A_("|---|---|---|---|")
    for x, y in PAIRS:
        p = f"{x}_vs_{y}"
        br, bp = boot_raw[p], boot_rep[p]
        fl = boot_flips[p]
        lab = ("**CHANGED**: " if fl["classification_changed"] else "unchanged: ")
        lab += (("sig" if fl["raw_significant"] else "ns") + " → "
                + ("sig" if fl["repaired_significant"] else "ns"))
        A_(f"| {x} − {y} | {br['rd']:+.3f} [{br['ci95_percentile'][0]:+.3f}, "
           f"{br['ci95_percentile'][1]:+.3f}] p={br['p_two_sided_percentile_inversion']:.4f} "
           f"| {bp['rd']:+.3f} [{bp['ci95_percentile'][0]:+.3f}, "
           f"{bp['ci95_percentile'][1]:+.3f}] p={bp['p_two_sided_percentile_inversion']:.4f} "
           f"| {lab} |")
    A_("")
    A_("## Which contrasts changed classification (α = .05)")
    A_("")
    changed_mcn = [p for p in flips if flips[p]["classification_changed"]]
    changed_boot = [p for p in boot_flips if boot_flips[p]["classification_changed"]]
    if changed_mcn:
        for p in changed_mcn:
            fl = flips[p]
            A_(f"- **McNemar {p.replace('_vs_', ' vs ')}**: "
               f"{'significant' if fl['raw_significant'] else 'not significant'} "
               f"under the raw instrument "
               f"(p={mcn_raw[p]['p_exact_binomial_two_sided']:.4g}) → "
               f"{'significant' if fl['repaired_significant'] else 'not significant'} "
               f"under the repaired instrument "
               f"(p={mcn_rep[p]['p_exact_binomial_two_sided']:.4g}).")
    else:
        A_("- No McNemar contrast changed classification.")
    if changed_boot:
        for p in changed_boot:
            fl = boot_flips[p]
            A_(f"- **Clustered bootstrap {p.replace('_vs_', ' vs ')}**: "
               f"{'significant' if fl['raw_significant'] else 'not significant'} "
               f"(p={boot_raw[p]['p_two_sided_percentile_inversion']:.4f}) → "
               f"{'significant' if fl['repaired_significant'] else 'not significant'} "
               f"(p={boot_rep[p]['p_two_sided_percentile_inversion']:.4f}).")
    else:
        A_("- No clustered-bootstrap contrast changed classification.")
    A_("")
    A_("## EVAL-341: known limitation")
    A_("")
    A_(f"Affected eval rows (produced ∧ no error ∧ raw-flagged ∧ repaired-clean): "
       f"A {len(eval_aff['A'])}, B {len(eval_aff['B'])}, C {len(eval_aff['C'])}, "
       f"D {len(eval_aff['D'])}, E {len(eval_aff['E'])} — {n_eval_aff} total, over "
       f"the {EVAL_BUDGET}-row typecheck budget, so the EVAL-341 table is NOT")
    A_("recomputed under the repaired instrument. These counts are hard upper")
    A_("bounds on each arm's possible success gain there and must be disclosed")
    A_("in the report: the same instrument bias direction (C/E flagged far more")
    A_("than D) applies to the eval table.")
    A_("")
    A_("## Provenance")
    A_("")
    A_("- Repaired oracle: `halluc_validation.py` `classify_adjusted` (R1 comments,")
    A_("  R2 imports, R3 self-declarations, R4 single-letter dot-notation heads")
    A_("  dropped; R5 single-segment-prefix namespace resolution → exists),")
    A_("  imported — not reimplemented. Blinded validation: 59/60 agreement.")
    A_("- Raw outcomes: `bridge_summary_v2.json` `paired_matrix` (untouched).")
    A_("- Typecheck of affected rows: `score_bridge.score_run` on the fresh-pin")
    A_(f"  REPL server ({pong['toolchain']}, mathlib {pong['mathlib_rev'][:12]}),")
    A_("  task={} (empty gold_header) — the identical Part-1 pipeline.")
    A_("- Bootstrap: `fresh_clustered.py` `cluster_boot_rd` imported; commit labels")
    A_("  from `fresh_tasks.jsonl`; raw replication asserted equal to")
    A_("  `fresh_clustered.json` cell by cell.")
    A_("- Per-row typecheck verdicts + hallucination-name diffs:")
    A_("  `success_repaired.json` → `affected_rows_detail`.")
    A_("")
    OUT_MD.write_text("\n".join(L))
    print(f"wrote {OUT_MD}")

    # ---- console summary ---------------------------------------------------
    print("\nFresh-100 both instruments (k/100 raw -> repaired):")
    for a in ARMS:
        print(f"  {a}: {tab_raw[a]['k']} -> {tab_rep[a]['k']} "
              f"(+{tab_rep[a]['k'] - tab_raw[a]['k']})")
    print("\nMcNemar p raw -> repaired:")
    for x, y in MCN_PAIRS:
        p = f"{x}_vs_{y}"
        print(f"  {p}: {mcn_raw[p]['p_exact_binomial_two_sided']:.4g} -> "
              f"{mcn_rep[p]['p_exact_binomial_two_sided']:.4g}")
    print("\nClustered bootstrap p raw -> repaired:")
    for x, y in PAIRS:
        p = f"{x}_vs_{y}"
        print(f"  {p}: {boot_raw[p]['p_two_sided_percentile_inversion']:.4f} -> "
              f"{boot_rep[p]['p_two_sided_percentile_inversion']:.4f}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
