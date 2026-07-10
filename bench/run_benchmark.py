#!/usr/bin/env python3
"""Run one Wikibrain benchmark arm by shelling the claude CLI per task.

Arms (same model, same prompt — the ONLY difference is tool availability):
  no_tools  — model alone: built-in tools disallowed, no MCP servers.
  wikibrain — same, plus the Wikibrain MCP server (mcp__wikibrain__* tools).

The claude CLI runs on Max auth: ANTHROPIC_API_KEY is REMOVED from the child
environment (CLAUDE.md Max-auth gotcha — a set key makes calls fail silently).
Both arms pass --strict-mcp-config so the user's own MCP servers never leak in,
and run from an empty working directory so file tools cannot see repo gold.

Results stream to bench/data/results_<arm>_<model>.jsonl, one row per task:
  {task_id, type, arm, model, answer_raw, answer_parsed, latency_s, n_turns,
   cost_usd, error?}
(--resume skips task ids that already have a non-error row.)

Examples:
  python3 bench/run_benchmark.py --arm no_tools --limit 5
  python3 bench/run_benchmark.py --arm wikibrain --resume
  WIKIBRAIN_MCP_URL=http://localhost:8787/mcp python3 bench/run_benchmark.py --arm wikibrain
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from tasklib import BENCH, DATA_DIR, TASKS_PATH, build_prompt, load_tasks, parse_answer  # noqa: E402

MCP_CONFIG_TEMPLATE = BENCH / "mcp-config.json"
DEFAULT_MODEL = "claude-haiku-4-5-20251001"

# The eight Wikibrain MCP tools (docs/BRAIN-V2.md axis 5) + the server-level
# rule as a catch-all if the tool list grows.
WIKIBRAIN_TOOLS = ["mcp__wikibrain"] + [
    f"mcp__wikibrain__{t}" for t in (
        "brain_search", "brain_node", "brain_unit", "brain_transfer",
        "brain_neighborhood", "brain_snippets", "brain_filter", "decl_exists")
]
# Belt-and-suspenders: -p mode auto-denies unapproved tools, but disallow the
# built-ins outright so neither arm can read files or search the web.
DISALLOWED_TOOLS = ["Bash", "Read", "Glob", "Grep", "Edit", "Write",
                    "NotebookEdit", "WebFetch", "WebSearch", "Task", "TodoWrite"]


def resolve_mcp_config() -> Path:
    cfg = json.loads(MCP_CONFIG_TEMPLATE.read_text())
    url = os.environ.get("WIKIBRAIN_MCP_URL")
    if url:
        cfg["mcpServers"]["wikibrain"]["url"] = url
    out = DATA_DIR / ".mcp-config.resolved.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(cfg, indent=2) + "\n")
    return out


def build_cmd(args: argparse.Namespace, prompt: str, mcp_config: Path | None) -> list[str]:
    # NB: no turn-limit flag — the installed claude CLI has none; --timeout
    # (wall clock per task) is the runaway bound.
    cmd = [args.claude_bin, "-p", prompt,
           "--output-format", "json",
           "--model", args.model,
           "--strict-mcp-config",
           "--disallowedTools", ",".join(DISALLOWED_TOOLS)]
    if args.arm == "wikibrain":
        cmd += ["--mcp-config", str(mcp_config),
                "--allowedTools", ",".join(WIKIBRAIN_TOOLS)]
    return cmd


def run_one(task: dict, args: argparse.Namespace, mcp_config: Path | None,
            env: dict, workdir: Path) -> dict:
    prompt = build_prompt(task)
    cmd = build_cmd(args, prompt, mcp_config)
    row = {"task_id": task["id"], "type": task["type"],
           "arm": args.arm, "model": args.model}
    t0 = time.monotonic()
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True,
                              timeout=args.timeout, env=env, cwd=workdir)
    except subprocess.TimeoutExpired:
        row.update({"answer_raw": None, "answer_parsed": None,
                    "latency_s": round(time.monotonic() - t0, 2),
                    "error": f"timeout after {args.timeout}s"})
        return row
    row["latency_s"] = round(time.monotonic() - t0, 2)
    try:
        data = json.loads(proc.stdout)
    except json.JSONDecodeError:
        row.update({"answer_raw": (proc.stdout or "")[:2000], "answer_parsed": None,
                    "error": f"non-JSON CLI output (rc={proc.returncode}): "
                             f"{(proc.stderr or '')[:300]}"})
        return row
    answer_raw = data.get("result") or ""
    row["answer_raw"] = answer_raw
    row["answer_parsed"] = parse_answer(task["type"], answer_raw)
    row["n_turns"] = data.get("num_turns")
    row["cost_usd"] = data.get("total_cost_usd")
    if data.get("is_error") or data.get("subtype") not in (None, "success"):
        row["error"] = f"CLI subtype={data.get('subtype')}"
    # 0-token silent failure (Max-auth gotcha symptom) => loud error row.
    if not answer_raw.strip():
        row["error"] = row.get("error") or "empty result"
    return row


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--arm", choices=["no_tools", "wikibrain"], required=True)
    ap.add_argument("--model", default=DEFAULT_MODEL)
    ap.add_argument("--tasks", type=Path, default=TASKS_PATH)
    ap.add_argument("--split", choices=["eval", "dev", "all"], default="eval")
    ap.add_argument("--limit", type=int, default=0,
                    help="run only the first N tasks (id order; 0 = all)")
    ap.add_argument("--concurrency", type=int, default=2)
    ap.add_argument("--resume", action="store_true",
                    help="skip task ids with a non-error row in the output file")
    ap.add_argument("--timeout", type=int, default=300, help="seconds per task")
    ap.add_argument("--claude-bin", default="claude")
    ap.add_argument("--dry-run", action="store_true",
                    help="print the resolved command for the first task and exit")
    args = ap.parse_args()

    if not args.dry_run and shutil.which(args.claude_bin) is None:
        print(f"error: claude CLI not found ({args.claude_bin!r})", file=sys.stderr)
        return 2

    tasks = [t for t in load_tasks(args.tasks)
             if args.split == "all" or t["split"] == args.split]
    tasks.sort(key=lambda t: t["id"])
    if args.limit:
        tasks = tasks[: args.limit]
    if not tasks:
        print("no tasks selected", file=sys.stderr)
        return 1

    mcp_config = resolve_mcp_config() if args.arm == "wikibrain" else None

    env = dict(os.environ)
    env.pop("ANTHROPIC_API_KEY", None)  # Max-auth gotcha (CLAUDE.md)
    workdir = DATA_DIR / ".workdir"
    workdir.mkdir(parents=True, exist_ok=True)

    out_path = DATA_DIR / f"results_{args.arm}_{args.model}.jsonl"
    done: set[str] = set()
    if args.resume and out_path.exists():
        with open(out_path) as f:
            for line in f:
                try:
                    r = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if r.get("task_id") and not r.get("error"):
                    done.add(r["task_id"])
    todo = [t for t in tasks if t["id"] not in done]

    if args.dry_run:
        t = todo[0] if todo else tasks[0]
        print("would run", len(todo), "tasks ->", out_path)
        print("cmd:", json.dumps(build_cmd(args, "<PROMPT>", mcp_config)))
        print("--- first prompt ---")
        print(build_prompt(t))
        return 0

    print(f"[{args.arm}/{args.model}] {len(todo)} tasks "
          f"({len(done)} resumed) -> {out_path}", file=sys.stderr)
    lock = threading.Lock()
    n_done = n_err = 0
    with open(out_path, "a") as out, ThreadPoolExecutor(args.concurrency) as ex:
        futs = {ex.submit(run_one, t, args, mcp_config, env, workdir): t
                for t in todo}
        for fut in as_completed(futs):
            row = fut.result()
            with lock:
                out.write(json.dumps(row, ensure_ascii=False) + "\n")
                out.flush()
                n_done += 1
                if row.get("error"):
                    n_err += 1
                print(f"  [{n_done}/{len(todo)}] {row['task_id']} "
                      f"{row['latency_s']}s"
                      + (f"  ERROR: {row['error']}" if row.get("error") else ""),
                      file=sys.stderr)
    print(f"done: {n_done} rows, {n_err} errors -> {out_path}", file=sys.stderr)
    return 0 if n_err == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
