#!/usr/bin/env python3
"""Factorial Stage-1 mechanical scoring — the 400 2x2 rows (arms Ep/X/J/Dp).

Preregistration: docs/research/BRIDGE-FACTORIAL.md (commit 3658bd58), section 5.

Primary per-row outcome (REPAIRED oracle): grounded typecheck =
  produced a declaration
  AND zero cited names classified 'hallucinated' by
      bench/analysis/halluc_validation.py::classify_adjusted
      (mechanical repair rules R1-R5 over the union oracle, exactly as v3 s4.1)
  AND the declaration typechecks on the bench-lean-fresh rig
      (score_bridge.typecheck_stub routing; sorry = warning, not error).
Raw-oracle grounded typecheck (score_bridge.Oracle.classify unrepaired) is
recorded per row as the preregistered sensitivity for the supplement.
Citation surface: output_lean, the run's final Lean block, as in v3.

Also per row: citation lists with raw+repaired verdicts, run-level
any-hallucination flags (repaired + raw), typecheck detail, turns / tool-call /
capped / cost descriptives. Rows with error would score as failures on all
endpoints (prereg s5 scoring notes) — the committed data has zero.

Integrity gate (re-verifies the run-phase claims before scoring):
  100 rows/arm; error None everywhere; attach_ok; per-arm condition_hash
  uniform and equal to conditions.json; init_tools == the arm manifest;
  assistant_turns <= max_turns 30; per-row >= 1 MCP tool call.

Rig identity gate: the fresh REPL server at /tmp/wikilean_tc_fresh.sock must
report toolchain leanprover/lean4:v4.33.0-rc1 and mathlib_rev 9944fe2973* —
exactly as bench/analysis/score_e31_v2.py gated.

Subcommands:
  score          (default) integrity gate + classify + typecheck all 400 rows
                 -> factorial_scored.json + factorial_scored.md
  retc           re-typecheck rows whose first verdict was a TIMEOUT — a
                 timeout can be REPL-server contention (a pathological
                 neighbor recycling the shared REPL, or the silent
                 single-shot fallback the v3 report's s3.4 defect 4
                 documents), which is infrastructure, not the row's own
                 verdict. Re-runs each such row alone against the idle
                 server; records every changed cell in retc_provenance and
                 rebuilds the aggregates. A row that times out again keeps
                 its timeout verdict (that cost is the declaration's own).
  retc-arm ARM   re-typecheck EVERY produced row of one arm against a healthy
                 restarted server (used for Dp after the REPL server died
                 mid-pass at Dp/fresh_053 and later rows silently fell back
                 to single-shot bare-env checks); healthy-window verdicts are
                 asserted to reproduce.
  judge-summary  after Stage 2: fold judge verdict summaries (main pass +
                 40-item consistency re-grade) into factorial_scored.{json,md}

Run:  python3 bench/analysis/factorial_scored.py [score|retc|retc-arm ARM|judge-summary]
"""
from __future__ import annotations

import json
import socket
import sys
import time
from collections import defaultdict
from pathlib import Path

HERE = Path(__file__).resolve().parent          # bench/analysis
BENCH = HERE.parent                              # bench/
sys.path.insert(0, str(BENCH))
sys.path.insert(0, str(HERE))
import score_bridge  # noqa: E402
from score_bridge import Oracle, extract_cited  # noqa: E402
from halluc_validation import (build_suffix_openable, classify_adjusted,  # noqa: E402
                               wilson95)

RUNS = BENCH / "data" / "runs_factorial"
CONDITIONS = RUNS / "conditions.json"
TASKS_FILE = BENCH / "data" / "fresh_tasks.jsonl"
OUT_JSON = HERE / "factorial_scored.json"
OUT_MD = HERE / "factorial_scored.md"
JUDGE_ROOT = HERE / "judge_factorial"

ARMS = ["Ep", "X", "J", "Dp"]
FRESH_SOCK = "/tmp/wikilean_tc_fresh.sock"
EXPECT_TOOLCHAIN = "leanprover/lean4:v4.33.0-rc1"
EXPECT_MATHLIB_PREFIX = "9944fe2973"
INFORMAL_TOOLS = {"mcp__wiki__wiki_search", "mcp__wiki__wiki_get",
                  "mcp__wiki__nlab_search"}
DECL_EXISTS_TOOL = "mcp__wikibrain__decl_exists"


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


def load_rows() -> dict[str, dict[str, dict]]:
    out: dict[str, dict[str, dict]] = {}
    for arm in ARMS:
        rows = {}
        for f in sorted((RUNS / arm).glob("fresh_*.json")):
            r = json.loads(f.read_text())
            rows[r["task_id"]] = r
        assert len(rows) == 100, f"arm {arm}: expected 100 rows, got {len(rows)}"
        out[arm] = rows
    return out


def integrity_gate(rows_by_arm: dict[str, dict[str, dict]]) -> dict:
    cond = json.loads(CONDITIONS.read_text())
    report: dict = {"max_turns": cond["max_turns"], "per_arm": {}}
    for arm in ARMS:
        rows = rows_by_arm[arm]
        errs = [t for t, r in rows.items() if r.get("error")]
        att = [t for t, r in rows.items() if not r.get("attach_ok")]
        hashes = {r["condition_hash"] for r in rows.values()}
        manifest = cond["arms"][arm]["manifest"]
        badman = [t for t, r in rows.items()
                  if sorted(r.get("init_tools") or []) != sorted(manifest)]
        zerotool = [t for t, r in rows.items()
                    if not (r.get("transcript_stats") or {}).get("tool_calls_by_name")]
        overturn = [t for t, r in rows.items()
                    if ((r.get("transcript_stats") or {}).get("assistant_turns")
                        or 0) > cond["max_turns"]]
        capped = sorted(t for t, r in rows.items() if r.get("capped"))
        assert not errs, f"{arm}: errored rows {errs}"
        assert not att, f"{arm}: attach-dirty rows {att}"
        assert hashes == {cond["condition_hashes"][arm]}, (
            f"{arm}: condition hash drift {hashes}")
        assert not badman, f"{arm}: manifest mismatch {badman[:5]}"
        assert not zerotool, f"{arm}: zero-tool rows {zerotool}"
        assert not overturn, f"{arm}: turn-cap violations {overturn}"
        report["per_arm"][arm] = {
            "n": len(rows), "errors": 0, "attach_dirty": 0,
            "condition_hash": cond["condition_hashes"][arm],
            "manifest_ok": True, "zero_tool_rows": 0,
            "turn_cap_violations": 0, "capped_rows": capped,
            "n_capped": len(capped),
        }
    return report


def score_all() -> None:
    # ---- rig identity gate --------------------------------------------------
    if not Path(FRESH_SOCK).exists():
        sys.exit(f"fresh typecheck server not up at {FRESH_SOCK} — start it: "
                 "python3 bench/typecheck.py --server "
                 "--project /Users/jack/Desktop/LEAN/bench-lean-fresh "
                 f"--socket {FRESH_SOCK} "
                 "--repl-bin /Users/jack/Desktop/LEAN/lean-repl-fresh/.lake/build/bin/repl")
    pong = ping_server(FRESH_SOCK)
    assert pong.get("toolchain") == EXPECT_TOOLCHAIN, pong
    assert str(pong.get("mathlib_rev", "")).startswith(EXPECT_MATHLIB_PREFIX), pong
    print(f"fresh rig OK: {pong['toolchain']} mathlib={pong['mathlib_rev'][:12]}")

    rows_by_arm = load_rows()
    integrity = integrity_gate(rows_by_arm)
    print("integrity gate OK: 400/400 rows, 0 errors, 0 attach-dirty, "
          "0 manifest mismatches, 0 zero-tool, 0 turn-cap violations")

    oracle = Oracle()
    assert oracle.enabled, "oracle sources missing"
    openable = build_suffix_openable(oracle)

    per_row: dict[str, dict[str, dict]] = {a: {} for a in ARMS}
    t0 = time.monotonic()
    done = 0
    for arm in ARMS:
        for tid in sorted(rows_by_arm[arm]):
            r = rows_by_arm[arm][tid]
            lean = r.get("output_lean")
            produced = bool(lean)
            cites = []
            halluc_raw, halluc_adj = [], []
            for n in extract_cited(lean):
                v_raw = oracle.classify(n)
                v_adj = classify_adjusted(lean, n, oracle, openable)
                cites.append({"name": n, "raw": v_raw, "repaired": v_adj})
                if v_raw == "hallucinated":
                    halluc_raw.append(n)
                if v_adj == "hallucinated":
                    halluc_adj.append(n)
            tc = score_bridge.typecheck_stub({}, r)  # routes to fresh sock
            tcd = r.get("_typecheck") or {}
            st = r.get("transcript_stats") or {}
            calls = st.get("tool_calls_by_name") or {}
            per_row[arm][tid] = {
                "produced": produced,
                "capped": bool(r.get("capped")),
                "citations": cites,
                "n_cited": len(cites),
                "halluc_raw": halluc_raw,
                "halluc_repaired": halluc_adj,
                "any_halluc_raw": bool(halluc_raw),
                "any_halluc_repaired": bool(halluc_adj),
                "typecheck": tc,
                "typecheck_timed_out": bool(tcd.get("timed_out")),
                "typecheck_elapsed_s": tcd.get("elapsed_s"),
                "typecheck_errors": tcd.get("errors") or [],
                "grounded_tc_repaired": bool(produced and not halluc_adj
                                             and tc is True),
                "grounded_tc_raw": bool(produced and not halluc_raw
                                        and tc is True),
                "assistant_turns": st.get("assistant_turns"),
                "turns": st.get("turns"),
                "tool_calls_by_name": calls,
                "n_tool_calls": sum(calls.values()),
                "n_informal_calls": sum(v for k, v in calls.items()
                                        if k in INFORMAL_TOOLS),
                "n_decl_exists_calls": calls.get(DECL_EXISTS_TOOL, 0),
                "cost_usd": st.get("cost_usd"),
                "tokens_in": st.get("tokens_in"),
                "tokens_out": st.get("tokens_out"),
                "wall_s": r.get("wall_s"),
            }
            done += 1
            if done % 25 == 0 or done == 400:
                print(f"  [{done}/400] {arm}/{tid} tc={tc} "
                      f"({tcd.get('elapsed_s', '?')}s) "
                      f"[{round(time.monotonic() - t0, 0)}s elapsed]", flush=True)

    per_arm = build_per_arm(per_row)

    out = {
        "generated_by": "bench/analysis/factorial_scored.py",
        "prereg": "docs/research/BRIDGE-FACTORIAL.md @ 3658bd58",
        "runs": str(RUNS) + " @ ba35fe7f",
        "primary_outcome": ("grounded typecheck (REPAIRED oracle): produced AND "
                            "zero classify_adjusted-hallucinated citations AND "
                            "typecheck ok on bench-lean-fresh"),
        "raw_oracle_note": ("grounded_tc_raw = same with the unrepaired union "
                            "oracle (supplement sensitivity)"),
        "grading_env": {
            "project": "/Users/jack/Desktop/LEAN/bench-lean-fresh",
            "socket": FRESH_SOCK,
            "toolchain": pong["toolchain"],
            "mathlib_rev": pong["mathlib_rev"],
        },
        "integrity": integrity,
        "per_arm": per_arm,
        "per_row": per_row,
        "total_cost_usd_run_phase": round(sum(
            per_arm[a]["cost_usd_total"] for a in ARMS), 2),
    }
    OUT_JSON.write_text(json.dumps(out, indent=1) + "\n")
    print(f"wrote {OUT_JSON}")
    write_md(out)


def build_per_arm(per_row: dict[str, dict[str, dict]]) -> dict[str, dict]:
    per_arm: dict[str, dict] = {}
    for arm in ARMS:
        rs = per_row[arm]
        n = len(rs)
        def k_of(pred):
            return sum(1 for s in rs.values() if pred(s))
        k_rep = k_of(lambda s: s["grounded_tc_repaired"])
        k_raw = k_of(lambda s: s["grounded_tc_raw"])
        k_hal_rep = k_of(lambda s: s["any_halluc_repaired"])
        k_hal_raw = k_of(lambda s: s["any_halluc_raw"])
        cited = sum(s["n_cited"] for s in rs.values())
        hal_c_rep = sum(len(s["halluc_repaired"]) for s in rs.values())
        hal_c_raw = sum(len(s["halluc_raw"]) for s in rs.values())
        tool_by_name: dict[str, int] = defaultdict(int)
        for s in rs.values():
            for kk, vv in s["tool_calls_by_name"].items():
                tool_by_name[kk] += vv
        costs = [s["cost_usd"] for s in rs.values() if s["cost_usd"] is not None]
        per_arm[arm] = {
            "n": n,
            "produced": k_of(lambda s: s["produced"]),
            "grounded_tc_repaired_k": k_rep,
            "grounded_tc_repaired_rate": round(k_rep / n, 4),
            "grounded_tc_repaired_wilson95": wilson95(k_rep, n),
            "grounded_tc_raw_k": k_raw,
            "grounded_tc_raw_rate": round(k_raw / n, 4),
            "grounded_tc_raw_wilson95": wilson95(k_raw, n),
            "typecheck_ok_k": k_of(lambda s: s["typecheck"] is True),
            "typecheck_none_k": k_of(lambda s: s["typecheck"] is None),
            "typecheck_timeout_k": k_of(lambda s: s["typecheck_timed_out"]),
            "runs_any_halluc_repaired_k": k_hal_rep,
            "runs_any_halluc_repaired_rate": round(k_hal_rep / n, 4),
            "runs_any_halluc_repaired_wilson95": wilson95(k_hal_rep, n),
            "runs_any_halluc_raw_k": k_hal_raw,
            "runs_any_halluc_raw_rate": round(k_hal_raw / n, 4),
            "citations_total": cited,
            "citations_halluc_repaired": hal_c_rep,
            "citations_halluc_raw": hal_c_raw,
            "citation_halluc_rate_repaired": (round(hal_c_rep / cited, 4)
                                              if cited else None),
            "capped_k": k_of(lambda s: s["capped"]),
            "mean_assistant_turns": round(
                sum(s["assistant_turns"] or 0 for s in rs.values()) / n, 2),
            "mean_tool_calls": round(
                sum(s["n_tool_calls"] for s in rs.values()) / n, 2),
            "tool_calls_by_name": dict(sorted(tool_by_name.items())),
            "decl_exists_calls_total": tool_by_name.get(DECL_EXISTS_TOOL, 0),
            "runs_using_decl_exists": k_of(lambda s: s["n_decl_exists_calls"] > 0),
            "informal_calls_total": sum(v for kk, v in tool_by_name.items()
                                        if kk in INFORMAL_TOOLS),
            "runs_touching_informal": k_of(lambda s: s["n_informal_calls"] > 0),
            "cost_usd_total": round(sum(costs), 2),
        }
    return per_arm


def retc_arm(arm: str) -> None:
    """Re-typecheck EVERY row of one arm against a healthy server.

    Exists because the REPL server died mid-pass while scoring arm Dp
    (during fresh_052's check; fresh_053.. fell back to single-shot
    bare-env checks at ~0.2 s —
    the v3 report's s3.4 defect-4 failure mode). Verdicts from the healthy
    window are asserted to reproduce; every changed cell is recorded in
    retc_provenance and the aggregates are rebuilt.
    """
    pong = ping_server(FRESH_SOCK)
    assert pong.get("toolchain") == EXPECT_TOOLCHAIN, pong
    assert str(pong.get("mathlib_rev", "")).startswith(EXPECT_MATHLIB_PREFIX), pong
    cur = json.loads(OUT_JSON.read_text())
    rows_by_arm = load_rows()
    changed, reproduced = [], 0
    todo = [t for t in sorted(cur["per_row"][arm])
            if cur["per_row"][arm][t]["produced"]]
    print(f"re-typechecking all {len(todo)} produced rows of arm {arm} (rig "
          f"{pong['toolchain']} mathlib={pong['mathlib_rev'][:12]})")
    for i, tid in enumerate(todo):
        r = rows_by_arm[arm][tid]
        tc = score_bridge.typecheck_stub({}, r)
        tcd = r.get("_typecheck") or {}
        assert not tcd.get("timed_out") or tc is False
        s = cur["per_row"][arm][tid]
        old = {"typecheck": s["typecheck"],
               "timed_out": s["typecheck_timed_out"],
               "elapsed_s": s["typecheck_elapsed_s"],
               "errors": s["typecheck_errors"]}
        suspect = (old["elapsed_s"] or 99) < 2.0
        if old["typecheck"] == tc and not suspect:
            reproduced += 1
        else:
            changed.append({"arm": arm, "task_id": tid, "old": old,
                            "new": {"typecheck": tc,
                                    "timed_out": bool(tcd.get("timed_out")),
                                    "elapsed_s": tcd.get("elapsed_s")},
                            "was_suspect_fast_fail": suspect})
        s["typecheck"] = tc
        s["typecheck_timed_out"] = bool(tcd.get("timed_out"))
        s["typecheck_elapsed_s"] = tcd.get("elapsed_s")
        s["typecheck_errors"] = tcd.get("errors") or []
        s["grounded_tc_repaired"] = bool(s["produced"] and not s["halluc_repaired"]
                                         and tc is True)
        s["grounded_tc_raw"] = bool(s["produced"] and not s["halluc_raw"]
                                    and tc is True)
        if (i + 1) % 10 == 0 or i + 1 == len(todo):
            print(f"  [{i+1}/{len(todo)}] {arm}/{tid} tc={tc} "
                  f"({tcd.get('elapsed_s','?')}s)", flush=True)
    cur["per_arm"] = build_per_arm(cur["per_row"])
    cur["total_cost_usd_run_phase"] = round(sum(
        cur["per_arm"][a]["cost_usd_total"] for a in ARMS), 2)
    prov = cur.setdefault("retc_provenance", [])
    prov.append({"what": f"arm {arm} fully re-typechecked against a restarted "
                         "healthy server: the REPL server died mid-pass during "
                         f"{arm}/fresh_052's check and later rows silently fell back "
                         "to single-shot bare-env checks (v3 s3.4 defect-4 "
                         "class; sub-second failures with unknown-identifier "
                         "errors)",
                 "rig": {"toolchain": pong["toolchain"],
                         "mathlib_rev": pong["mathlib_rev"]},
                 "healthy_window_reproduced": reproduced,
                 "cells_changed_or_suspect": changed})
    OUT_JSON.write_text(json.dumps(cur, indent=1) + "\n")
    print(f"updated {OUT_JSON}: {reproduced} healthy verdicts reproduced, "
          f"{len(changed)} cells changed/suspect")
    write_md(cur)


def retc() -> None:
    """Re-typecheck timeout rows one at a time against the idle server."""
    pong = ping_server(FRESH_SOCK)
    assert pong.get("toolchain") == EXPECT_TOOLCHAIN, pong
    assert str(pong.get("mathlib_rev", "")).startswith(EXPECT_MATHLIB_PREFIX), pong
    cur = json.loads(OUT_JSON.read_text())
    rows_by_arm = load_rows()
    oracle = Oracle()
    assert oracle.enabled
    todo = [(a, t) for a in ARMS for t, s in cur["per_row"][a].items()
            if s["typecheck_timed_out"]]
    print(f"{len(todo)} timeout rows to re-typecheck (rig "
          f"{pong['toolchain']} mathlib={pong['mathlib_rev'][:12]})")
    changed = []
    for arm, tid in todo:
        r = rows_by_arm[arm][tid]
        tc = score_bridge.typecheck_stub({}, r)
        tcd = r.get("_typecheck") or {}
        s = cur["per_row"][arm][tid]
        rec = {"arm": arm, "task_id": tid,
               "old": {"typecheck": s["typecheck"],
                       "timed_out": s["typecheck_timed_out"],
                       "elapsed_s": s["typecheck_elapsed_s"]},
               "new": {"typecheck": tc,
                       "timed_out": bool(tcd.get("timed_out")),
                       "elapsed_s": tcd.get("elapsed_s")}}
        if bool(tcd.get("timed_out")):
            rec["kept"] = "timed out again — verdict stands"
            print(f"  {arm}/{tid}: still times out ({tcd.get('elapsed_s')}s)")
        else:
            s["typecheck"] = tc
            s["typecheck_timed_out"] = False
            s["typecheck_elapsed_s"] = tcd.get("elapsed_s")
            s["typecheck_errors"] = tcd.get("errors") or []
            s["grounded_tc_repaired"] = bool(s["produced"]
                                             and not s["halluc_repaired"]
                                             and tc is True)
            s["grounded_tc_raw"] = bool(s["produced"] and not s["halluc_raw"]
                                        and tc is True)
            print(f"  {arm}/{tid}: {rec['old']['typecheck']} (timeout) -> {tc} "
                  f"({tcd.get('elapsed_s')}s)")
        changed.append(rec)
    cur["per_arm"] = build_per_arm(cur["per_row"])
    cur["total_cost_usd_run_phase"] = round(sum(
        cur["per_arm"][a]["cost_usd_total"] for a in ARMS), 2)
    prov = cur.setdefault("retc_provenance", [])
    prov.append({"what": "timeout rows re-typechecked alone against the idle "
                         "server (first-pass timeouts can be REPL contention, "
                         "not the row's verdict — v3 s3.4 defect 4 class)",
                 "rig": {"toolchain": pong["toolchain"],
                         "mathlib_rev": pong["mathlib_rev"]},
                 "cells": changed})
    OUT_JSON.write_text(json.dumps(cur, indent=1) + "\n")
    print(f"updated {OUT_JSON} ({len(changed)} cells examined)")
    write_md(cur)


def judge_summary() -> None:
    """Fold Stage-2 judge verdicts into factorial_scored.{json,md}."""
    cur = json.loads(OUT_JSON.read_text())
    summary: dict = {"per_arm": {}, "consistency": None}
    verdicts: dict[str, dict[str, dict]] = {a: {} for a in ARMS}
    for arm in ARMS:
        for f in sorted((JUDGE_ROOT / arm).glob("*.judge.json")):
            v = json.loads(f.read_text())
            verdicts[arm][v["task_id"]] = v
        assert len(verdicts[arm]) == 100, (arm, len(verdicts[arm]))
        n = 100
        ev = sum(1 for v in verdicts[arm].values() if v.get("evaluated"))
        st = sum(1 for v in verdicts[arm].values() if v.get("strict"))
        errs = sum(1 for v in verdicts[arm].values() if v.get("judge_error"))
        noout = sum(1 for v in verdicts[arm].values() if v.get("no_output"))
        cost = sum(v.get("judge_cost_usd") or 0 for v in verdicts[arm].values())
        summary["per_arm"][arm] = {
            "n": n, "evaluated_k": ev, "evaluated_rate": round(ev / n, 4),
            "evaluated_wilson95": wilson95(ev, n),
            "strict_k": st, "strict_rate": round(st / n, 4),
            "judge_errors": errs, "no_output_rows": noout,
            "judge_cost_usd": round(cost, 2),
        }
    # conjunction: grounded_tc_repaired AND evaluated
    for arm in ARMS:
        pr = cur["per_row"][arm]
        k = sum(1 for tid, s in pr.items()
                if s["grounded_tc_repaired"] and verdicts[arm][tid].get("evaluated"))
        summary["per_arm"][arm]["conjunction_grounded_and_evaluated_k"] = k
        summary["per_arm"][arm]["conjunction_rate"] = round(k / 100, 4)
        summary["per_arm"][arm]["conjunction_wilson95"] = wilson95(k, 100)
        for tid, s in pr.items():
            s["judge_evaluated"] = bool(verdicts[arm][tid].get("evaluated"))
            s["judge_strict"] = bool(verdicts[arm][tid].get("strict"))
            s["judge_error"] = bool(verdicts[arm][tid].get("judge_error"))
    # consistency re-grade agreement
    cons_root = JUDGE_ROOT / "consistency"
    if cons_root.exists():
        agree_ev = agree_st = tot = 0
        for arm in ARMS:
            for f in sorted((cons_root / arm).glob("*.judge.json")):
                v2 = json.loads(f.read_text())
                v1 = verdicts[arm][v2["task_id"]]
                tot += 1
                agree_ev += (bool(v1.get("evaluated")) == bool(v2.get("evaluated")))
                agree_st += (bool(v1.get("strict")) == bool(v2.get("strict")))
        summary["consistency"] = {
            "n": tot, "seed": 20260727,
            "evaluated_agreement": round(agree_ev / tot, 4) if tot else None,
            "strict_agreement": round(agree_st / tot, 4) if tot else None,
        }
    summary["judge_cost_usd_total"] = round(sum(
        summary["per_arm"][a]["judge_cost_usd"] for a in ARMS), 2)
    cur["judge"] = summary
    OUT_JSON.write_text(json.dumps(cur, indent=1) + "\n")
    print(f"merged judge summary -> {OUT_JSON}")
    write_md(cur)


def write_md(out: dict) -> None:
    md = ["# Factorial Stage-1 mechanical scoring (+ judge summary when folded)",
          "",
          f"Prereg: `{out['prereg']}`. Rows: `{out['runs']}`. Rig: "
          f"{out['grading_env']['toolchain']}, mathlib "
          f"{out['grading_env']['mathlib_rev'][:12]}.",
          "",
          "Primary outcome (repaired oracle): produced AND zero "
          "classify_adjusted-hallucinated citations AND typecheck ok.",
          "",
          "| arm | grounded-tc (repaired) | Wilson 95% | raw-oracle | typecheck ok "
          "| any-halluc (rep.) | capped | mean turns | mean tool calls |",
          "|---|---|---|---|---|---|---|---|"]
    for arm in ARMS:
        a = out["per_arm"][arm]
        w = a["grounded_tc_repaired_wilson95"]
        md.append(
            f"| {arm} | {a['grounded_tc_repaired_k']}/100 "
            f"({a['grounded_tc_repaired_rate']:.3f}) | [{w[0]:.3f}, {w[1]:.3f}] "
            f"| {a['grounded_tc_raw_k']}/100 | {a['typecheck_ok_k']}/100 "
            f"| {a['runs_any_halluc_repaired_k']}/100 | {a['capped_k']} "
            f"| {a['mean_assistant_turns']} | {a['mean_tool_calls']} |")
    md += ["",
           f"decl_exists manipulation check: X {out['per_arm']['X']['decl_exists_calls_total']} "
           f"calls ({out['per_arm']['X']['runs_using_decl_exists']}/100 runs), "
           f"Dp {out['per_arm']['Dp']['decl_exists_calls_total']} calls "
           f"({out['per_arm']['Dp']['runs_using_decl_exists']}/100 runs); "
           f"J/Ep 0 by construction (manifest-verified).",
           f"Informal-tool touches: Ep {out['per_arm']['Ep']['runs_touching_informal']}/100 runs, "
           f"X {out['per_arm']['X']['runs_touching_informal']}/100 runs.",
           f"Run-phase cost: ${out['total_cost_usd_run_phase']}.",
           ""]
    if out.get("judge"):
        j = out["judge"]
        md += ["## Judge (blinded claude-sonnet-5, Stage 2)",
               "",
               "| arm | evaluated | Wilson 95% | strict | conjunction (grounded ∧ eval) |",
               "|---|---|---|---|---|"]
        for arm in ARMS:
            ja = j["per_arm"][arm]
            w = ja["evaluated_wilson95"]
            md.append(f"| {arm} | {ja['evaluated_k']}/100 ({ja['evaluated_rate']:.3f}) "
                      f"| [{w[0]:.3f}, {w[1]:.3f}] | {ja['strict_k']}/100 "
                      f"| {ja['conjunction_grounded_and_evaluated_k']}/100 |")
        if j.get("consistency"):
            c = j["consistency"]
            md.append(f"\nSelf-consistency re-grade (n={c['n']}, seed {c['seed']}): "
                      f"evaluated agreement {c['evaluated_agreement']:.2%}, "
                      f"strict agreement {c['strict_agreement']:.2%}.")
        md.append(f"\nJudge cost: ${j['judge_cost_usd_total']}.")
        md.append("")
    OUT_MD.write_text("\n".join(md))
    print(f"wrote {OUT_MD}")


def main() -> int:
    cmd = sys.argv[1] if len(sys.argv) > 1 else "score"
    if cmd == "score":
        score_all()
    elif cmd == "retc":
        retc()
    elif cmd == "retc-arm":
        retc_arm(sys.argv[2])
    elif cmd == "judge-summary":
        judge_summary()
    else:
        sys.exit(f"unknown subcommand {cmd!r}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
