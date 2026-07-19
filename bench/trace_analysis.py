#!/usr/bin/env python3
"""Trace attribution for the Bridge Experiment (deviation 7 telemetry).

For every run that carries a tool_trace (eval arms C/D/E + all fresh runs),
ask: did the decls the agent ultimately cited first surface in a TOOL RESULT
(attributable retrieval), merely appear in a tool INPUT (the agent brought the
name and checked it), or never touch the tools at all (memory)?

Caveat, stated everywhere it matters: result_head is truncated to 200 chars,
so result-attribution is an UNDERCOUNT — "appeared in a result" is reliable
evidence FOR retrieval; its absence is not proof of memory.

Usage: python3 bench/trace_analysis.py [--runs-dir bench/data/runs]
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from score_bridge import extract_cited, Oracle  # noqa: E402

# The informal->formal jump tools (arm D); everything else is formal-only or
# generic search. Attribution to THESE is the hypothesis-relevant signal.
BRIDGE_TOOLS = {"mcp__wikibrain__brain_bridge", "mcp__wikibrain__brain_transfer"}
CHECK_TOOLS = {"mcp__wikibrain__decl_exists", "mcp__formal__loogle",
               "mcp__formal__decl_grep", "mcp__formal__decl_read"}


def analyze_run(run: dict, oracle: Oracle) -> dict | None:
    trace = run.get("tool_trace") or []
    if not trace:
        return None
    cited = extract_cited(run.get("output_lean"))
    if not cited:
        return None
    in_result: set[str] = set()
    in_result_bridge: set[str] = set()
    in_input_check: set[str] = set()
    for e in trace:
        head = e.get("result_head") or ""
        inp = e.get("input") or ""
        for d in cited:
            if d in head:
                in_result.add(d)
                if e.get("name") in BRIDGE_TOOLS:
                    in_result_bridge.add(d)
            if e.get("name") in CHECK_TOOLS and d in inp:
                in_input_check.add(d)
    halluc = {d for d in cited if oracle.classify(d) == "hallucinated"}
    return {
        "n_cited": len(cited),
        "n_in_result": len(in_result),
        "n_in_result_bridge": len(in_result_bridge),
        "n_checked": len(in_input_check),
        "n_untouched": len([d for d in cited
                            if d not in in_result and d not in in_input_check]),
        "n_halluc": len(halluc),
        "n_halluc_checked": len(halluc & in_input_check),
        "any_result_attributed": bool(in_result),
        "any_bridge_attributed": bool(in_result_bridge),
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--runs-dir", type=Path, default=HERE / "data" / "runs")
    args = ap.parse_args()
    oracle = Oracle()
    print(f"oracle: {'ON' if oracle.enabled else 'OFF — decl classes unavailable'}")
    print("(result-attribution is an UNDERCOUNT: result_head is 200-char truncated)\n")
    for armdir in sorted(args.runs_dir.iterdir()):
        if not armdir.is_dir():
            continue
        agg = defaultdict(int)
        n = 0
        for f in sorted(armdir.glob("*.json")):
            try:
                run = json.loads(f.read_text())
            except (OSError, json.JSONDecodeError):
                continue
            a = analyze_run(run, oracle)
            if a is None:
                continue
            n += 1
            for k, v in a.items():
                agg[k] += int(v)
        if not n:
            continue
        print(f"arm {armdir.name}: {n} traced runs with citations")
        print(f"  runs w/ >=1 cited decl seen in a TOOL RESULT: "
              f"{agg['any_result_attributed']}/{n} "
              f"({agg['any_result_attributed']/n*100:.0f}%)")
        if agg["any_bridge_attributed"]:
            print(f"  runs w/ >=1 cited decl from a BRIDGE result "
                  f"(brain_bridge/transfer): {agg['any_bridge_attributed']}/{n} "
                  f"({agg['any_bridge_attributed']/n*100:.0f}%)")
        tot = agg["n_cited"]
        print(f"  cited decls: {tot}; in-result {agg['n_in_result']} "
              f"({agg['n_in_result']/tot*100:.0f}%), checked-via-input "
              f"{agg['n_checked']} ({agg['n_checked']/tot*100:.0f}%), "
              f"untouched-by-tools {agg['n_untouched']} "
              f"({agg['n_untouched']/tot*100:.0f}%)")
        if agg["n_halluc"]:
            print(f"  hallucinated citations: {agg['n_halluc']}; of those, "
                  f"CHECKED-and-cited-anyway: {agg['n_halluc_checked']}")
        print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
