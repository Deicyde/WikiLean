#!/usr/bin/env python3
"""Bridge v2 — AGENT mode: Sonnet ± retrieval tools produces ranked decl lists.

Arms (tools are the ONLY difference; model fixed per Jack 2026-07-25):
  N — no tools at all (--tools "")
  W — wikibrain MCP (bench/arms/mcp-D.json -> local worker /mcp)
  F — formal search MCP (bench/arms/mcp-C.json: loogle/decl_grep/decl_read)

Task contract (same for every arm): given a benchmark query, return a JSON
array of <=10 fully-qualified Mathlib declaration names, most-relevant first,
as the FINAL message. qr810 = "identify the described declaration";
mpr = "list the premises needed to prove this theorem".

MAX TELEMETRY (Jack: "collect as much data as possible"):
  - the COMPLETE raw stream-json transcript is retained per run, gzipped:
      runs/agent/<bench>/<arm>/<model>/<qid>.stream.jsonl.gz
  - the derived row carries tool_trace with caps far above Tier-1's
    (INPUT_TRUNC 2000, RESULT_TRUNC 4000, TRACE_CAP 400) + turns/tokens/cost.
Runs dirs are model-keyed — a second-model grid never overwrites the first.

Usage:
  python3 bench/v2/run_agent.py --bench mpr   --arm W [--concurrency 6]
  python3 bench/v2/run_agent.py --bench qr810 --arm N F W
Resumable: existing row files are skipped.
"""
from __future__ import annotations

import argparse
import gzip
import json
import re
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

HERE = Path(__file__).resolve().parent
BENCH = HERE.parent
sys.path.insert(0, str(BENCH))
sys.path.insert(0, str(HERE))
from run_benchmark import DISALLOWED_TOOLS  # noqa: E402 — the sealed list
from run_bridge import resolve_arm_config  # noqa: E402
from score_retrieval import qr_rows, mpr_rows  # noqa: E402

import os  # noqa: E402


def scrub_env() -> dict:
    """Max-auth child env (CLAUDE.md gotcha + the 644fd89 endpoint scrub)."""
    env = dict(os.environ)
    for k in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL",
              "USE_STAGING_OAUTH", "USE_LOCAL_OAUTH", "CLAUDE_CODE_OAUTH_SCOPES"):
        env.pop(k, None)
    return env

DEFAULT_MODEL = "claude-sonnet-5"
TRACE_CAP, INPUT_TRUNC, RESULT_TRUNC = 400, 2000, 4000

ARM_CFG = {
    "N": {"mcp": None, "tools": []},
    "W": {"mcp": "D", "tools": [
        "mcp__wikibrain", "mcp__wikibrain__brain_bridge",
        "mcp__wikibrain__brain_search", "mcp__wikibrain__brain_cell",
        "mcp__wikibrain__brain_transfer", "mcp__wikibrain__brain_neighborhood",
        "mcp__wikibrain__brain_snippets", "mcp__wikibrain__brain_filter",
        "mcp__wikibrain__decl_exists"]},
    "F": {"mcp": "C", "tools": [
        "mcp__formal", "mcp__formal__loogle", "mcp__formal__decl_grep",
        "mcp__formal__decl_read"]},
}


def build_prompt(bench: str, query: str) -> str:
    if bench == "qr810":
        task = (f"A Mathlib4 declaration is described as:\n\n  {query}\n\n"
                "Identify which existing Mathlib declaration this describes.")
    else:
        task = ("You are preparing to prove this theorem in Lean 4 with "
                f"Mathlib:\n\n{query}\n\nList the existing Mathlib premises "
                "(lemmas/definitions the proof will invoke).")
    return (f"{task}\n\n"
            "Reply with ONLY a JSON array of at most 10 fully-qualified "
            "Mathlib declaration names, most relevant first, e.g. "
            '["Order.Lattice.foo", "Nat.bar"]. No prose, no code fences '
            "around anything except the array itself. Never invent names — "
            "prefer fewer, real declarations over ten guesses.")


def extract_ranked(text: str) -> list[str]:
    for m in reversed(list(re.finditer(r"\[[^\[\]]*\]", text, re.S))):
        try:
            arr = json.loads(m.group(0))
        except json.JSONDecodeError:
            continue
        if isinstance(arr, list) and all(isinstance(x, str) for x in arr):
            return arr[:10]
    return []


def parse_stream(stdout: str) -> dict:
    """run_bridge.parse_stream with raised caps + nothing discarded upstream
    (the raw stream is saved separately by the caller)."""
    result_text, subtype, is_error = "", None, None
    turns = cost = tin = tout = None
    tool_calls: dict[str, int] = {}
    trace: list[dict] = []
    by_id: dict[str, dict] = {}
    for line in stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            continue
        et = ev.get("type")
        if et == "assistant":
            for blk in (ev.get("message") or {}).get("content", []) or []:
                if isinstance(blk, dict) and blk.get("type") == "tool_use":
                    nm = blk.get("name", "?")
                    tool_calls[nm] = tool_calls.get(nm, 0) + 1
                    if len(trace) < TRACE_CAP:
                        e = {"i": len(trace), "name": nm,
                             "input": json.dumps(blk.get("input"),
                                                 ensure_ascii=False)[:INPUT_TRUNC]}
                        trace.append(e)
                        if blk.get("id"):
                            by_id[blk["id"]] = e
        elif et == "user":
            for blk in (ev.get("message") or {}).get("content", []) or []:
                if isinstance(blk, dict) and blk.get("type") == "tool_result":
                    e = by_id.get(blk.get("tool_use_id") or "")
                    if e is not None:
                        c = blk.get("content")
                        s = c if isinstance(c, str) else json.dumps(c, ensure_ascii=False)
                        e["ok"] = not bool(blk.get("is_error"))
                        e["result_chars"] = len(s)
                        e["result_head"] = s[:RESULT_TRUNC]
        elif et == "result":
            result_text = ev.get("result") or result_text
            subtype = ev.get("subtype")
            is_error = ev.get("is_error")
            turns = ev.get("num_turns", turns)
            cost = ev.get("total_cost_usd", cost)
            u = ev.get("usage") or {}
            tin, tout = u.get("input_tokens", tin), u.get("output_tokens", tout)
    return {"result_text": result_text, "subtype": subtype, "is_error": is_error,
            "turns": turns, "cost_usd": cost, "tokens_in": tin,
            "tokens_out": tout, "tool_calls_by_name": tool_calls,
            "tool_trace": trace}


def run_one(bench: str, arm: str, model: str, qid: str, query: str,
            out_dir: Path, mcp_config: Path | None, timeout: int) -> str:
    cmd = ["claude", "-p", build_prompt(bench, query),
           "--output-format", "stream-json", "--verbose", "--model", model,
           "--strict-mcp-config", "--disallowedTools", ",".join(DISALLOWED_TOOLS)]
    cfg = ARM_CFG[arm]
    if mcp_config is not None:
        cmd += ["--mcp-config", str(mcp_config),
                "--allowedTools", ",".join(cfg["tools"])]
    else:
        cmd += ["--tools", ""]
    t0 = time.monotonic()
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True,
                              timeout=timeout, env=scrub_env())
        stdout = proc.stdout
        err = None
    except subprocess.TimeoutExpired as e:
        stdout = (e.stdout or b"").decode() if isinstance(e.stdout, bytes) else (e.stdout or "")
        err = f"timeout after {timeout}s"
    with gzip.open(out_dir / f"{qid}.stream.jsonl.gz", "wt") as f:
        f.write(stdout)
    st = parse_stream(stdout)
    ranked = extract_ranked(st["result_text"])
    row = {"qid": qid, "bench": bench, "arm": arm, "model": model,
           "query": query, "ranked": ranked,
           "wall_s": round(time.monotonic() - t0, 1),
           "transcript_stats": {k: st[k] for k in
                                ("turns", "tokens_in", "tokens_out", "cost_usd",
                                 "tool_calls_by_name")},
           "tool_trace": st["tool_trace"]}
    if err or st["is_error"] or not ranked:
        row["error"] = err or ("CLI error" if st["is_error"] else
                               "no JSON array in final message")
    (out_dir / f"{qid}.json").write_text(json.dumps(row) + "\n")
    n_tools = sum(st["tool_calls_by_name"].values())
    return f"{qid} {row['wall_s']}s tools={n_tools} ranked={len(ranked)}" + \
           (f" ERR:{row.get('error')}" if row.get("error") else "")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--bench", choices=["qr810", "mpr"], required=True)
    ap.add_argument("--arm", nargs="+", choices=list(ARM_CFG), required=True)
    ap.add_argument("--model", default=DEFAULT_MODEL)
    ap.add_argument("--concurrency", type=int, default=6)
    ap.add_argument("--timeout", type=int, default=420)
    args = ap.parse_args()

    items = ([(r["qid"], r["query"]) for r in qr_rows()] if args.bench == "qr810"
             else [(r["qid"], r["nl"]) for r in mpr_rows()])
    for arm in args.arm:
        out_dir = HERE / "runs" / "agent" / args.bench / arm / args.model
        out_dir.mkdir(parents=True, exist_ok=True)
        todo = [(q, t) for q, t in items if not (out_dir / f"{q}.json").exists()]
        mcp = resolve_arm_config(ARM_CFG[arm]["mcp"]) if ARM_CFG[arm]["mcp"] else None
        print(f"[{args.bench}/{arm}/{args.model}] {len(todo)} to run "
              f"({len(items) - len(todo)} resumed) -> {out_dir}", file=sys.stderr)
        with ThreadPoolExecutor(max_workers=args.concurrency) as ex:
            futs = {ex.submit(run_one, args.bench, arm, args.model, q, t,
                              out_dir, mcp, args.timeout): q for q, t in todo}
            for i, fut in enumerate(as_completed(futs), 1):
                print(f"  [{i}/{len(todo)}] {fut.result()}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
