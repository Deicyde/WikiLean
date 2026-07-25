#!/usr/bin/env python3
"""SorryDB agent phase: Sonnet ± tools drafts a replacement for one `sorry`.

Kernel-graded later (verify_sorrydb.py builds each repo once and checks every
candidate in-situ) — no judge anywhere. This phase needs NO repo builds: the
task row carries the goal state + a ±60-line context window at the pinned
commit (bench/v2/data/sorrydb/tasks_frozen.jsonl, built by sorrydb_prep.py).

Arms (same semantics as run_agent.py): N none · F formal search · WF union +
AGENT_MANUAL.md. Model default claude-sonnet-5. MAX TELEMETRY: full raw
stream-json per run (gzipped) + tool_trace + tokens/cost per row — cost is a
first-class reported metric (Jack 2026-07-25).

Output contract for the agent: ONE fenced ```lean block whose content
replaces the `sorry` exactly (a tactic block or term), nothing else after it.

Usage: python3 bench/v2/run_sorrydb.py --arm N F WF [--concurrency 4]
Rows -> bench/v2/runs/sorrydb/<arm>/<model>/<id>.json (resumable).
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
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent))
from run_agent import (  # noqa: E402 — shared harness
    ARM_CFG, MANUAL, MANUAL_ARMS, DISALLOWED_TOOLS, parse_stream, scrub_env,
    resolve_arm_config,
)

TASKS = HERE / "data" / "sorrydb" / "tasks_frozen.jsonl"
DEFAULT_MODEL = "claude-sonnet-5"


def build_prompt(t: dict, arm: str) -> str:
    goal = t.get("goal") or "(no recorded goal state)"
    head = (
        f"Repository: {t['repo']} @ {t['commit']} (Lean {t['lean_version']})\n"
        f"File: {t['path']}\n"
        f"A `sorry` sits at line {t['start_line']}, column {t['start_column']}.\n\n"
        f"PROOF GOAL at the sorry:\n```\n{goal}\n```\n\n"
        f"FILE CONTEXT (line-numbered, ±60 lines):\n```lean\n{t['context_window']}\n```\n\n"
        "Write the Lean 4 proof that REPLACES the `sorry`. Rules:\n"
        "- Reply with exactly ONE fenced ```lean block containing ONLY the "
        "replacement text (a term, or a `by` tactic block); no other code.\n"
        "- It must elaborate in THIS repository at THIS commit: use only "
        "declarations that exist there (the repo's own + its Mathlib version "
        "if it depends on Mathlib). Mind version drift: current Mathlib may "
        "differ from this pin.\n"
        "- No `sorry`, no `admit`, no new axioms.\n"
        "- If after honest effort you cannot find a proof, reply with a "
        "```lean block containing exactly `sorry` — an honest pass beats a "
        "fake proof."
    )
    if arm in MANUAL_ARMS:
        head = f"{MANUAL}\n\n---\n\n{head}"
    return head


def extract_proof(text: str) -> str | None:
    blocks = re.findall(r"```(?:lean4?)?\s*\n(.*?)```", text or "", re.S)
    if not blocks:
        return None
    return blocks[-1].strip() or None


def run_one(t: dict, arm: str, model: str, out_dir: Path,
            mcp_config: Path | None, timeout: int) -> str:
    prompt = build_prompt(t, arm)
    cmd = ["claude", "-p", prompt, "--output-format", "stream-json", "--verbose",
           "--model", model, "--strict-mcp-config",
           "--disallowedTools", ",".join(DISALLOWED_TOOLS)]
    if mcp_config is not None:
        cmd += ["--mcp-config", str(mcp_config),
                "--allowedTools", ",".join(ARM_CFG[arm]["tools"])]
    else:
        cmd += ["--tools", ""]
    t0 = time.monotonic()
    err = None
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True,
                              timeout=timeout, env=scrub_env())
        stdout = proc.stdout
    except subprocess.TimeoutExpired as e:
        stdout = e.stdout if isinstance(e.stdout, str) else \
            (e.stdout or b"").decode("utf-8", "replace")
        err = f"timeout after {timeout}s"
    tid = t["id"]
    with gzip.open(out_dir / f"{tid}.stream.jsonl.gz", "wt") as f:
        f.write(stdout or "")
    st = parse_stream(stdout or "")
    proof = extract_proof(st["result_text"])
    row = {"id": tid, "arm": arm, "model": model, "repo": t["repo"],
           "commit": t["commit"], "path": t["path"],
           "start_line": t["start_line"], "start_column": t["start_column"],
           "end_line": t["end_line"], "end_column": t["end_column"],
           "proof": proof, "gave_up": (proof or "").strip() == "sorry",
           "wall_s": round(time.monotonic() - t0, 1),
           "transcript_stats": {k: st[k] for k in
                                ("turns", "tokens_in", "tokens_out", "cost_usd",
                                 "tool_calls_by_name")},
           "tool_trace": st["tool_trace"]}
    if err or st["is_error"] or proof is None:
        row["error"] = err or ("CLI error" if st["is_error"] else
                               "no lean block in final message")
    (out_dir / f"{tid}.json").write_text(json.dumps(row) + "\n")
    nt = sum((st["tool_calls_by_name"] or {}).values())
    tag = "GAVE_UP" if row["gave_up"] else f"{len(proof or '')}ch"
    return f"{tid} {row['wall_s']}s tools={nt} proof={tag}" + \
           (f" ERR:{row.get('error')}" if row.get("error") else "")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--arm", nargs="+", choices=list(ARM_CFG), required=True)
    ap.add_argument("--model", default=DEFAULT_MODEL)
    ap.add_argument("--concurrency", type=int, default=4)
    ap.add_argument("--timeout", type=int, default=600)
    args = ap.parse_args()
    tasks = [json.loads(l) for l in TASKS.read_text().splitlines() if l.strip()]
    for arm in args.arm:
        out_dir = HERE / "runs" / "sorrydb" / arm / args.model
        out_dir.mkdir(parents=True, exist_ok=True)
        todo = [t for t in tasks if not (out_dir / f"{t['id']}.json").exists()]
        mcp = resolve_arm_config(ARM_CFG[arm]["mcp"]) if ARM_CFG[arm]["mcp"] else None
        print(f"[sorrydb/{arm}/{args.model}] {len(todo)} to run "
              f"({len(tasks) - len(todo)} resumed)", file=sys.stderr)
        with ThreadPoolExecutor(max_workers=args.concurrency) as ex:
            futs = {ex.submit(run_one, t, arm, args.model, out_dir, mcp,
                              args.timeout): t["id"] for t in todo}
            for i, fut in enumerate(as_completed(futs), 1):
                try:
                    print(f"  [{i}/{len(todo)}] {fut.result()}", file=sys.stderr)
                except Exception as e:  # noqa: BLE001 — never lose the arm to one row
                    print(f"  [{i}/{len(todo)}] {futs[fut]} EXC:{e}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
