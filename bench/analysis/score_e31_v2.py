#!/usr/bin/env python3
"""Part 1 of the report-v2 grading job: mechanically score the 31 repaired arm-E
fresh rows (fresh_069..fresh_099, rerun 2026-07-27 after the original 429-error
block) and produce bench/analysis/bridge_summary_v2.json.

Everything reuses bench/score_bridge.py verbatim (Oracle, extract_cited,
score_run -> typecheck_stub -> fresh-pin REPL server), so the 31 new rows are
graded by the IDENTICAL pipeline that produced bench/data/bridge_summary.json:

  - tasks are loaded from bridge_tasks.jsonl only (fresh rows score with
    task={}, i.e. empty gold_header), exactly as the original pass did;
  - typecheck routes to the fresh pin via /tmp/wikilean_tc_fresh.sock
    (bench-lean-fresh: toolchain v4.33.0-rc1, mathlib_rev 9944fe2973) —
    score_bridge._route_for does this automatically for fresh_* task ids;
  - success = produced ∧ no error ∧ zero hallucinated citations ∧ typecheck ok.

bridge_summary_v2.json = deep copy of bridge_summary.json with
  * paired_matrix E cells for the 31 tasks replaced,
  * every McNemar pair involving E recomputed from the patched matrix
    (non-E pairs recomputed too and asserted unchanged),
  * arms.E aggregates recomputed (citation stats re-derived over all 471 E rows
    and validated against the v1 aggregate on the pre-repair rows; typecheck
    counts patched additively since the 31 old rows had typecheck=None),
  * a "v2_provenance" block naming each changed cell and how it moved.
bridge_summary.json itself is NOT touched.

Also writes part1_fresh100_v2.{json,md}: the updated fresh-100 grounded-
typecheck table (Wilson 95% CIs) + McNemars D-vs-E and E-vs-A/B/C (and D-vs-C,
D-vs-A) on the full 100 pairs, alongside the completed-69 numbers copied from
tier1_reanalysis.json for continuity.

Run:  python3 bench/analysis/score_e31_v2.py
Requires the fresh typecheck server up:
  python3 bench/typecheck.py --server \
    --project /Users/jack/Desktop/LEAN/bench-lean-fresh \
    --socket /tmp/wikilean_tc_fresh.sock \
    --repl-bin /Users/jack/Desktop/LEAN/lean-repl-fresh/.lake/build/bin/repl
"""
from __future__ import annotations

import copy
import json
import socket
import sys
from collections import defaultdict
from pathlib import Path

HERE = Path(__file__).resolve().parent          # bench/analysis
BENCH = HERE.parent                              # bench/
sys.path.insert(0, str(BENCH))
import score_bridge  # noqa: E402
from tier1_reanalysis import mcnemar_exact, paired_rd_wald, wilson_ci  # noqa: E402

RUNS_E = BENCH / "data" / "runs" / "E"
ARCHIVE = BENCH / "data" / "runs_E_fresh_429_archive"
SUMMARY_V1 = BENCH / "data" / "bridge_summary.json"
TIER1 = HERE / "tier1_reanalysis.json"
OUT_V2 = HERE / "bridge_summary_v2.json"
OUT_F100_JSON = HERE / "part1_fresh100_v2.json"
OUT_F100_MD = HERE / "part1_fresh100_v2.md"

E31_IDS = [f"fresh_{i:03d}" for i in range(69, 100)]
ARMS = ["A", "B", "C", "D", "E"]
FRESH_SOCK = "/tmp/wikilean_tc_fresh.sock"
EXPECT_TOOLCHAIN = "leanprover/lean4:v4.33.0-rc1"
EXPECT_MATHLIB_PREFIX = "9944fe2973"


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


def cheap_stats(row: dict, oracle) -> dict:
    """The oracle/citation legs of score_bridge.score_run, WITHOUT typecheck."""
    lean = row.get("output_lean")
    errored = bool(row.get("error"))
    cited = score_bridge.extract_cited(lean)
    cls = {n: oracle.classify(n) for n in cited}
    exists = [n for n, c in cls.items() if c == "exists"]
    renamed = [n for n, c in cls.items() if c == "renamed"]
    halluc = [n for n, c in cls.items() if c == "hallucinated"]
    resolved = len(exists) + len(renamed)
    denom = resolved + len(halluc)
    return {
        "produced": bool(lean), "errored": errored,
        "n_cited": len(cited), "n_hallucinated": len(halluc),
        "hallucinated_names": halluc,
        "decl_existence_rate": (round(resolved / denom, 4) if denom else None),
        "success_proxy": bool(lean) and not errored and not halluc,
        "stats": row.get("transcript_stats") or {},
    }


def main() -> int:
    # ---- rig identity gate --------------------------------------------------
    if not Path(FRESH_SOCK).exists():
        sys.exit(f"fresh typecheck server not up at {FRESH_SOCK} — see docstring")
    pong = ping_server(FRESH_SOCK)
    assert pong.get("toolchain") == EXPECT_TOOLCHAIN, pong
    assert str(pong.get("mathlib_rev", "")).startswith(EXPECT_MATHLIB_PREFIX), pong
    print(f"fresh rig OK: {pong['toolchain']} mathlib={pong['mathlib_rev'][:12]}")

    v1 = json.loads(SUMMARY_V1.read_text())
    oracle = score_bridge.Oracle(enabled=True)
    assert oracle.enabled, "decl oracle failed to load"
    tasks = score_bridge.load_tasks(score_bridge.DEFAULT_TASKS)  # fresh -> {}

    # ---- load all 471 E rows (current tree) --------------------------------
    e_rows: dict[str, dict] = {}
    for f in sorted(RUNS_E.glob("*.json")):
        if ".judge" in f.name:
            continue
        row = json.loads(f.read_text())
        e_rows[row.get("task_id") or f.stem] = row
    assert len(e_rows) == v1["arms"]["E"]["n"] == 471, len(e_rows)
    for tid in E31_IDS:
        assert not e_rows[tid].get("error"), f"{tid} still errored"
        assert e_rows[tid].get("output_lean"), f"{tid} has no output"

    # ---- validation: pre-repair citation stats must reproduce v1 -----------
    # (440 unchanged rows + the 31 archived 429 rows == the population v1 scored)
    old_pop = {}
    for tid, row in e_rows.items():
        old_pop[tid] = row
    for tid in E31_IDS:
        old_pop[tid] = json.loads((ARCHIVE / f"{tid}.json").read_text())
    ost = {t: cheap_stats(r, oracle) for t, r in old_pop.items()}
    v1e = v1["arms"]["E"]
    checks = {
        "produced": sum(s["produced"] for s in ost.values()),
        "success_proxy_k": sum(s["success_proxy"] for s in ost.values()),
        "cited_total": sum(s["n_cited"] for s in ost.values()),
        "hallucinated_total": sum(s["n_hallucinated"] for s in ost.values()),
        "runs_with_hallucination": sum(s["n_hallucinated"] > 0 for s in ost.values()),
    }
    for k, got in checks.items():
        assert got == v1e[k], f"oracle drift on {k}: recomputed {got} != v1 {v1e[k]}"
    rates = [s["decl_existence_rate"] for s in ost.values()
             if s["decl_existence_rate"] is not None]
    assert round(sum(rates) / len(rates), 4) == v1e["mean_decl_existence_rate"], \
        "oracle drift on mean_decl_existence_rate"
    print("v1 reproduction check: pre-repair E citation stats match bridge_summary.json")

    # ---- score the 31 new rows (identical pipeline, WITH typecheck) --------
    print(f"typechecking {len(E31_IDS)} repaired rows on the fresh pin ...")
    scored31: dict[str, dict] = {}
    for i, tid in enumerate(E31_IDS):
        s = score_bridge.score_run(tasks.get(tid, {}), e_rows[tid], oracle)
        scored31[tid] = s
        print(f"  [{i+1:2d}/31] {tid}: tc={s['typecheck']} "
              f"halluc={s['n_hallucinated']} success={s['success']} "
              f"({(e_rows[tid].get('_typecheck') or {}).get('elapsed_s', '?')}s)")
    assert all(s["typecheck"] is not None for s in scored31.values())

    # ---- build v2 ----------------------------------------------------------
    v2 = copy.deepcopy(v1)

    # paired matrix
    moved = []
    for tid in E31_IDS:
        old = v1["paired_matrix"][tid]["E"]
        new = scored31[tid]["success"]
        v2["paired_matrix"][tid]["E"] = new
        moved.append({
            "task_id": tid, "arm": "E",
            "old_folded_success": old, "new_folded_success": new,
            "moved": f"{old} -> {new}",
            "old_row": "429 session-limit error, no output (archived)",
            "new_success_proxy": scored31[tid]["success_proxy"],
            "new_typecheck_ok": scored31[tid]["typecheck"],
            "new_n_hallucinated": scored31[tid]["n_hallucinated"],
            "new_hallucinated_names": scored31[tid]["hallucinated_names"],
            "new_typecheck_errors": ((e_rows[tid].get("_typecheck") or {})
                                     .get("errors") or []
                                     if scored31[tid]["typecheck"] is not True
                                     else []),
        })

    # arms.E aggregate
    nst = {t: cheap_stats(r, oracle) for t, r in e_rows.items()}
    n = len(nst)
    new31_tc_ok = sum(1 for s in scored31.values() if s["typecheck"] is True)
    new31_tc_none = sum(1 for s in scored31.values() if s["typecheck"] is None)
    new31_tc_to = sum(1 for s in scored31.values() if s["typecheck_timed_out"])
    new31_success = sum(1 for s in scored31.values() if s["success"])
    rates2 = [s["decl_existence_rate"] for s in nst.values()
              if s["decl_existence_rate"] is not None]
    calls = [sum((s["stats"].get("tool_calls_by_name") or {}).values())
             for s in nst.values()]
    tin = [s["stats"].get("tokens_in") for s in nst.values()
           if s["stats"].get("tokens_in") is not None]
    tout = [s["stats"].get("tokens_out") for s in nst.values()
            if s["stats"].get("tokens_out") is not None]
    tool_by_name: dict[str, int] = defaultdict(int)
    for s in nst.values():
        for k, v in (s["stats"].get("tool_calls_by_name") or {}).items():
            tool_by_name[k] += v
    e2 = dict(v1e)
    e2.update({
        "produced": sum(s["produced"] for s in nst.values()),
        "success_proxy_k": sum(s["success_proxy"] for s in nst.values()),
        "success_k": v1e["success_k"] + new31_success,           # old 31 were False
        "typecheck_ok_k": v1e["typecheck_ok_k"] + new31_tc_ok,   # old 31 were None
        "typecheck_none_k": v1e["typecheck_none_k"] - 31 + new31_tc_none,
        "typecheck_timeout_k": v1e["typecheck_timeout_k"] + new31_tc_to,
        "runs_with_hallucination": sum(s["n_hallucinated"] > 0 for s in nst.values()),
        "cited_total": sum(s["n_cited"] for s in nst.values()),
        "hallucinated_total": sum(s["n_hallucinated"] for s in nst.values()),
        "mean_decl_existence_rate": round(sum(rates2) / len(rates2), 4),
        "mean_tool_calls": round(sum(calls) / n, 2),
        "tool_calls_by_name": dict(sorted(tool_by_name.items())),
        "mean_tokens_out": round(sum(tout) / len(tout), 1) if tout else None,
        "mean_tokens_in": round(sum(tin) / len(tin), 1) if tin else None,
    })
    e2["success_proxy_rate"] = round(e2["success_proxy_k"] / n, 4)
    e2["success_rate"] = round(e2["success_k"] / n, 4)
    e2["hallucinated_decl_rate"] = (round(e2["hallucinated_total"] /
                                          e2["cited_total"], 4)
                                    if e2["cited_total"] else None)
    # cross-check success_k against the patched matrix
    mat_e_k = sum(1 for t in v2["paired_matrix"]
                  if v2["paired_matrix"][t]["E"] is True)
    assert mat_e_k == e2["success_k"], (mat_e_k, e2["success_k"])
    v2["arms"]["E"] = e2

    # McNemar blocks: recompute every existing pair from the patched matrix;
    # assert pairs not involving E are unchanged.
    def mcn_from_matrix(matrix: dict, x: str, y: str) -> dict:
        both = xonly = yonly = neither = 0
        n_paired = 0
        for t, row in matrix.items():
            sx, sy = row.get(x), row.get(y)
            if sx is None or sy is None:
                continue
            n_paired += 1
            if sx and sy:
                both += 1
            elif sx:
                xonly += 1
            elif sy:
                yonly += 1
            else:
                neither += 1
        return {"n_paired": n_paired, "both_success": both,
                "x_only": xonly, "y_only": yonly, "neither": neither,
                "discordant": xonly + yonly,
                "note": v1["mcnemar"]["D_vs_E"]["note"]}

    for pair in list(v2["mcnemar"]):
        x, y = pair.split("_vs_")
        new_block = mcn_from_matrix(v2["paired_matrix"], x, y)
        if "E" not in (x, y):
            old = {k: v for k, v in v1["mcnemar"][pair].items()}
            assert new_block == old, f"non-E pair {pair} changed: {new_block} vs {old}"
        v2["mcnemar"][pair] = new_block

    v2["v2_provenance"] = {
        "generated_by": "bench/analysis/score_e31_v2.py",
        "date": "2026-07-27",
        "base": str(SUMMARY_V1),
        "what_changed": ("The 31 arm-E fresh rows fresh_069..fresh_099 (originally "
                         "429 session-limit errors, archived in "
                         "bench/data/runs_E_fresh_429_archive/) were rerun "
                         "2026-07-27 via bench/analysis/rerun_E_fresh.py and are "
                         "scored here with the identical score_bridge.py pipeline "
                         "(Oracle citation classification + fresh-pin REPL "
                         "typecheck). Only these 31 (arm, task) cells changed; "
                         "arms.E aggregates and every McNemar pair involving E "
                         "were recomputed; all other cells and pairs verified "
                         "unchanged."),
        "grading_env": {
            "project": "/Users/jack/Desktop/LEAN/bench-lean-fresh",
            "socket": FRESH_SOCK,
            "toolchain": pong["toolchain"],
            "mathlib_rev": pong["mathlib_rev"],
        },
        "v1_reproduction_check": ("pre-repair E population (440 current + 31 "
                                  "archived rows) re-derived citation stats "
                                  "match bridge_summary.json arms.E exactly"),
        "changed_cells": moved,
    }
    OUT_V2.write_text(json.dumps(v2, indent=2, ensure_ascii=False) + "\n")
    print(f"wrote {OUT_V2}")

    # ---- fresh-100 tables + McNemars (report block) ------------------------
    tier1 = json.loads(TIER1.read_text())
    fresh_ids = sorted(t for t in v2["paired_matrix"] if t.startswith("fresh_"))
    assert len(fresh_ids) == 100
    completed_ids = sorted(set(fresh_ids) - set(E31_IDS))
    assert len(completed_ids) == 69

    def table(ids):
        out = {}
        for a in ARMS:
            k = sum(1 for t in ids if v2["paired_matrix"][t][a] is True)
            lo, hi = wilson_ci(k, len(ids))
            out[a] = {"k": k, "n": len(ids), "rate": round(k / len(ids), 4),
                      "wilson95": [round(lo, 4), round(hi, 4)]}
        return out

    def mcn(ids, x, y):
        both = xonly = yonly = neither = 0
        for t in ids:
            sx = bool(v2["paired_matrix"][t][x])
            sy = bool(v2["paired_matrix"][t][y])
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

    pairs = ["D_vs_E", "E_vs_A", "E_vs_B", "E_vs_C", "D_vs_C", "D_vs_A"]
    mcn100 = {p: mcn(fresh_ids, *p.split("_vs_")) for p in pairs}
    m_de = mcn100["D_vs_E"]
    rd = paired_rd_wald(m_de["D_only"], m_de["E_only"], 100)
    f100 = {
        "generated_by": "bench/analysis/score_e31_v2.py",
        "outcome_source": str(OUT_V2),
        "success_metric": v2["success_metric"],
        "fresh_100_table_v2": table(fresh_ids),
        "mcnemar_fresh_100_v2": mcn100,
        "effect_size_D_vs_E_fresh100_v2": {
            "rd_D_minus_E": round(rd["rd"], 4), "se": round(rd["se"], 4),
            "rd_ci95": [round(rd["ci95"][0], 4), round(rd["ci95"][1], 4)],
            "method": rd["method"]},
        "completed_69_v2_check": table(completed_ids),
        "continuity_completed_69_from_tier1_reanalysis": {
            "table": tier1["completed_69_table"],
            "mcnemar": tier1["mcnemar_completed_69"]},
        "continuity_fresh_100_v1_errors_as_failures": {
            "table": tier1["fresh_100_table"],
            "mcnemar": tier1["mcnemar_fresh_100"]},
    }
    # the 69 completed pairs are untouched by the repair — must equal tier1's table
    for a in ARMS:
        t1 = tier1["completed_69_table"][a]
        assert (f100["completed_69_v2_check"][a]["k"], f100["completed_69_v2_check"][a]["n"]) \
            == (t1["k"], t1["n"]), (a, f100["completed_69_v2_check"][a], t1)
    OUT_F100_JSON.write_text(json.dumps(f100, indent=2) + "\n")

    md = ["# Part 1 — fresh-100 grounded-typecheck, arm-E 429 block repaired (v2)\n",
          f"Reproduce: `python3 bench/analysis/score_e31_v2.py` (fresh rig: "
          f"{pong['toolchain']}, mathlib {pong['mathlib_rev'][:12]}; metric: "
          f"{v2['success_metric']}).\n",
          "## Fresh-100 grounded-typecheck (all arms, all rows completed)\n",
          "| arm | k/n | rate | Wilson 95% CI |", "|---|---|---|---|"]
    for a in ARMS:
        r = f100["fresh_100_table_v2"][a]
        md.append(f"| {a} | {r['k']}/{r['n']} | {r['rate']:.3f} "
                  f"| [{r['wilson95'][0]:.3f}, {r['wilson95'][1]:.3f}] |")
    md.append("\n## McNemar (exact binomial two-sided), full 100 pairs\n")
    for p in pairs:
        b = mcn100[p]
        x, y = p.split("_vs_")
        md.append(f"- **{x} vs {y}**: both={b['both_success']}, {x}-only={b[f'{x}_only']}, "
                  f"{y}-only={b[f'{y}_only']}, neither={b['neither']} -> "
                  f"p = **{b['p_exact_binomial_two_sided']:.4g}**")
    es = f100["effect_size_D_vs_E_fresh100_v2"]
    md.append(f"\nD-vs-E absolute risk difference (paired Wald): "
              f"**{es['rd_D_minus_E']:+.3f}** 95% CI [{es['rd_ci95'][0]:+.3f}, "
              f"{es['rd_ci95'][1]:+.3f}]\n")
    md.append("## Continuity: completed-69 (from tier1_reanalysis.json, unchanged)\n")
    md.append("| arm | k/n | rate | Wilson 95% CI |")
    md.append("|---|---|---|---|")
    for a in ARMS:
        r = tier1["completed_69_table"][a]
        md.append(f"| {a} | {r['k']}/{r['n']} | {r['rate']:.3f} "
                  f"| [{r['wilson95'][0]:.3f}, {r['wilson95'][1]:.3f}] |")
    md.append("\nMcNemar on the 69 completed pairs (tier1_reanalysis.json):\n")
    for p in ("D_vs_E", "D_vs_C", "D_vs_A"):
        b = tier1["mcnemar_completed_69"][p]
        x, y = p.split("_vs_")
        md.append(f"- **{x} vs {y}**: both={b['both_success']}, {x}-only={b[f'{x}_only']}, "
                  f"{y}-only={b[f'{y}_only']}, neither={b['neither']} -> "
                  f"p = {b['p_exact_binomial_two_sided']:.4g}")
    n_moved = sum(1 for m in moved if m["new_folded_success"])
    md.append(f"\n(Of the 31 repaired E rows, {n_moved} became successes; "
              f"{31 - n_moved} remain failures. Per-cell detail: "
              f"bridge_summary_v2.json `v2_provenance.changed_cells`.)\n")
    OUT_F100_MD.write_text("\n".join(md))
    print(f"wrote {OUT_F100_JSON}\nwrote {OUT_F100_MD}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
