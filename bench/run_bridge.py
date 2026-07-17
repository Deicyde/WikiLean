#!/usr/bin/env python3
"""Run one Bridge-Experiment arm over the statement-autoformalization task set.

Five arms differ ONLY in the tool manifest (identical model, prompt, budgets —
docs/research/BRIDGE-EXPERIMENT.md):

  A  no_tools     no MCP servers (floor)
  B  informal     wiki_mcp     (Wikipedia + nLab; no Lean)
  C  formal       formal_mcp   (Loogle + Mathlib grep/read; no concept layer)
  D  wikibrain    the remote Wikibrain MCP (the bridge)
  E  b+c-unjoined wiki_mcp + formal_mcp, NO wikibrain (the decisive control)

Tier-1 task (bench/data/bridge_tasks.jsonl, first line optional {"_meta":…}):
  {id, informal_statement, gold_formal, gold_header, split, domain}
The agent produces ONE Lean 4 declaration ending `:= sorry`. Grading is POST-HOC
(bench/score_bridge.py) — typecheck-in-the-loop is NOT available this campaign,
so the prompt tells the agent it cannot compile and must cite only decls it has
verified exist via its tools.

The claude CLI runs on Max auth: ANTHROPIC_API_KEY is REMOVED from the child env
(CLAUDE.md Max-auth gotcha). Every arm passes --strict-mcp-config and runs from a
fresh empty temp dir OUTSIDE the repo so repo CLAUDE.md / .claude hooks+skills /
cwd-keyed auto-memory cannot contaminate any arm (same isolation as
run_benchmark.py). Arm D preflights the MCP server (initialize over HTTP) and
aborts loudly if unreachable — the CLI otherwise degrades silently to no-tools.

Per task, writes bench/data/runs/<arm>/<task_id>.json:
  {task_id, arm, model, output_lean, transcript_stats:{turns, tool_calls_by_name,
   tokens_in, tokens_out, cost_usd}, wall_s, timestamp, max_turns, error?}
(--resume skips task ids that already have a non-error file.)

Examples:
  python3 bench/run_bridge.py --arm A --limit 5
  python3 bench/run_bridge.py --arm E --split dev --resume
  python3 bench/run_bridge.py --arm A --dry-run
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from tasklib import BENCH, DATA_DIR, REPO  # noqa: E402
# Reuse the sibling runner's Max-auth-safe machinery verbatim.
from run_benchmark import (  # noqa: E402
    DISALLOWED_TOOLS, PREFLIGHT_TIMEOUT_S, WIKIBRAIN_TOOLS, preflight_wikibrain,
)

ARMS_DIR = BENCH / "arms"
BRIDGE_TASKS = DATA_DIR / "bridge_tasks.jsonl"
RUNS_DIR = DATA_DIR / "runs"
DEFAULT_MODEL = "claude-haiku-4-5-20251001"
MATHLIB_ROOT = "/Users/jack/Desktop/LEAN/mathlib4/Mathlib"

# Per-arm MCP config (repo-relative; None = arm A, no servers) + allowed tools.
# Server names below MUST match the mcpServers keys in bench/arms/mcp-*.json.
WIKI_TOOLS = ["mcp__wiki", "mcp__wiki__wiki_search", "mcp__wiki__wiki_get",
              "mcp__wiki__nlab_search"]
FORMAL_TOOLS = ["mcp__formal", "mcp__formal__loogle", "mcp__formal__decl_grep",
                "mcp__formal__decl_read"]
ARM_CONFIG = {"A": None, "B": "mcp-B.json", "C": "mcp-C.json",
              "D": "mcp-D.json", "E": "mcp-E.json"}
ARM_TOOLS = {"A": [], "B": WIKI_TOOLS, "C": FORMAL_TOOLS,
             "D": WIKIBRAIN_TOOLS, "E": WIKI_TOOLS + FORMAL_TOOLS}

# A synthetic task so --dry-run works before bench/data/bridge_tasks.jsonl exists
# (that file is built by a parallel agent). NEVER used by a real run.
SAMPLE_TASK = {
    "id": "SAMPLE-0001",
    "informal_statement": "Every compact subset of a Hausdorff topological space is closed.",
    "gold_formal": "theorem IsCompact.isClosed {X : Type*} [TopologicalSpace X] "
                   "[T2Space X] {s : Set X} (hs : IsCompact s) : IsClosed s := sorry",
    "gold_header": "IsCompact.isClosed",
    "split": "dev", "domain": "topology",
}


# --------------------------------------------------------------------------- #
# Prompt — arm-neutral, IDENTICAL across arms (only the tool manifest differs). #
# Deliberately does NOT name Wikibrain or any specific tool.                    #
# --------------------------------------------------------------------------- #
def build_prompt(task: dict, max_turns: int) -> str:
    # informal_context (when present) is agent-visible problem framing; the gold
    # NL proof (informal_proof_gold) is reference-only and NEVER shown — showing
    # it would contaminate the treatment (bridge_tasks _meta).
    statement = task["informal_statement"].strip()
    ctx = (task.get("informal_context") or "").strip()
    if ctx:
        statement = f"{ctx}\n\n{statement}"
    return "\n".join([
        "You are formalizing an informal mathematical statement into Lean 4, "
        "targeting Mathlib4 (leanprover-community/mathlib4).",
        "",
        "Informal statement:",
        statement,
        "",
        "Task: produce EXACTLY ONE Lean 4 declaration whose signature faithfully "
        "captures this statement, with its proof replaced by `:= sorry`. Use a "
        "`theorem` (or `def`/`lemma` if the statement is a definition). It must be "
        "a single self-contained declaration ending in `:= sorry`.",
        "",
        "You CANNOT compile or typecheck your answer — no Lean toolchain is "
        "available in this session, and your answer is graded later. Therefore "
        "cite only declarations, definitions, and notation you have VERIFIED "
        "exist; never invent or guess a declaration name. If you cannot confirm a "
        "specific name, prefer a more general Mathlib concept you have confirmed, "
        "or express the hypothesis in elementary terms.",
        "",
        "Some tools for searching mathematical references and/or the Lean library "
        "may be available to you this session. If so, use them to ground every "
        f"name you cite (you have a budget of about {max_turns} tool-using turns). "
        "If no tools are available, rely on your own knowledge and stay "
        "conservative — do not fabricate names.",
        "",
        "Your reply MUST end with a single fenced Lean code block containing "
        "exactly one declaration ending in `:= sorry`:",
        "```lean",
        "theorem some_name (...) : ... := sorry",
        "```",
    ])


_LEAN_BLOCK_RE = re.compile(r"```(?:lean|lean4)?\s*\n(.*?)```", re.DOTALL | re.IGNORECASE)


def extract_lean(text: str | None) -> str | None:
    """Pull the produced declaration: the last fenced ```lean block that contains
    `sorry`, else the last fenced block, else a `:= sorry`-bearing line span."""
    if not text:
        return None
    blocks = [b.strip() for b in _LEAN_BLOCK_RE.findall(text)]
    if blocks:
        withsorry = [b for b in blocks if "sorry" in b]
        return (withsorry[-1] if withsorry else blocks[-1]) or None
    # No fence: grab from the last decl keyword through the sorry.
    m = re.search(r"((?:theorem|lemma|def|abbrev|instance)\b.*?:=\s*sorry)",
                  text, re.DOTALL | re.IGNORECASE)
    return m.group(1).strip() if m else None


# --------------------------------------------------------------------------- #
# Config resolution (absolutize stdio script paths; inject env; apply URL).     #
# --------------------------------------------------------------------------- #
def resolve_arm_config(arm: str) -> Path | None:
    name = ARM_CONFIG[arm]
    if name is None:
        return None
    cfg = json.loads((ARMS_DIR / name).read_text())
    cfg.pop("_arm", None)
    servers = cfg.get("mcpServers", {})
    url_override = os.environ.get("WIKIBRAIN_MCP_URL")
    for _, spec in servers.items():
        if spec.get("type") == "http":
            if url_override:
                spec["url"] = url_override
        else:  # stdio: absolutize the script path, pass the checkout + rg shim env
            spec["args"] = [str((REPO / a).resolve()) if a.endswith(".py") else a
                            for a in spec.get("args", [])]
            env = dict(spec.get("env") or {})
            env.setdefault("MATHLIB_ROOT", MATHLIB_ROOT)
            claude_exec = os.environ.get("CLAUDE_CODE_EXECPATH") or shutil.which("claude")
            if claude_exec:
                env.setdefault("CLAUDE_CODE_EXECPATH", claude_exec)
            spec["env"] = env
    out = DATA_DIR / f".mcp-bridge-{arm}.resolved.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(cfg, indent=2) + "\n")
    return out


def build_cmd(args: argparse.Namespace, prompt: str, mcp_config: Path | None) -> list[str]:
    cmd = [args.claude_bin, "-p", prompt,
           "--output-format", "stream-json", "--verbose",
           "--model", args.model,
           "--strict-mcp-config",
           "--disallowedTools", ",".join(DISALLOWED_TOOLS)]
    tools = ARM_TOOLS[args.arm]
    if mcp_config is not None:
        cmd += ["--mcp-config", str(mcp_config), "--allowedTools", ",".join(tools)]
    else:
        # Arm A: allowlist-only empty built-in set (no MCP servers at all).
        cmd += ["--tools", ""]
    return cmd


# --------------------------------------------------------------------------- #
# stream-json parsing → transcript stats                                        #
# --------------------------------------------------------------------------- #
def parse_stream(stdout: str) -> dict:
    """Fold the newline-delimited stream-json events into one stats dict.
    Robust to unexpected/extra event types; missing fields degrade to None."""
    result_text, subtype, is_error, api_err = "", None, None, None
    turns = cost = tin = tout = None
    tool_calls: dict[str, int] = {}
    for line in stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            continue
        etype = ev.get("type")
        if etype == "assistant":
            for blk in (ev.get("message") or {}).get("content", []) or []:
                if isinstance(blk, dict) and blk.get("type") == "tool_use":
                    nm = blk.get("name", "?")
                    tool_calls[nm] = tool_calls.get(nm, 0) + 1
        elif etype == "result":
            result_text = ev.get("result") or result_text
            subtype = ev.get("subtype")
            is_error = ev.get("is_error")
            api_err = ev.get("api_error_status", api_err)
            turns = ev.get("num_turns", turns)
            cost = ev.get("total_cost_usd", cost)
            usage = ev.get("usage") or {}
            tin = usage.get("input_tokens", tin)
            tout = usage.get("output_tokens", tout)
    return {"result_text": result_text, "subtype": subtype, "is_error": is_error,
            "api_error_status": api_err, "turns": turns, "cost_usd": cost,
            "tokens_in": tin, "tokens_out": tout, "tool_calls_by_name": tool_calls}


def now_ts() -> str:
    try:
        return subprocess.run(["date", "-u", "+%Y-%m-%dT%H:%M:%SZ"],
                              capture_output=True, text=True, timeout=5).stdout.strip()
    except (subprocess.SubprocessError, OSError):
        return ""


def out_path(arm: str, task_id: str) -> Path:
    return RUNS_DIR / arm / f"{task_id}.json"


def run_one(task: dict, args: argparse.Namespace, mcp_config: Path | None,
            env: dict, workdir: Path) -> dict:
    prompt = build_prompt(task, args.max_turns)
    cmd = build_cmd(args, prompt, mcp_config)
    row: dict = {"task_id": task["id"], "arm": args.arm, "model": args.model,
                 "max_turns": args.max_turns, "timestamp": now_ts()}
    t0 = time.monotonic()
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True,
                              timeout=args.timeout, env=env, cwd=workdir)
    except subprocess.TimeoutExpired:
        row.update({"output_lean": None, "wall_s": round(time.monotonic() - t0, 1),
                    "transcript_stats": {}, "error": f"timeout after {args.timeout}s"})
        return row
    row["wall_s"] = round(time.monotonic() - t0, 1)
    stats = parse_stream(proc.stdout)
    row["output_lean"] = extract_lean(stats["result_text"])
    row["transcript_stats"] = {
        "turns": stats["turns"], "tool_calls_by_name": stats["tool_calls_by_name"],
        "tokens_in": stats["tokens_in"], "tokens_out": stats["tokens_out"],
        "cost_usd": stats["cost_usd"]}
    if stats["is_error"] or stats["subtype"] not in (None, "success"):
        detail = (stats["result_text"] or "").strip().replace("\n", " ")[:200]
        api = f" api_status={stats['api_error_status']}" if stats["api_error_status"] else ""
        row["error"] = f"CLI error (subtype={stats['subtype']}{api}): {detail}"
    if not (stats["result_text"] or "").strip():
        row["error"] = row.get("error") or "empty result (0-token Max-auth symptom?)"
    if row.get("output_lean") is None and not row.get("error"):
        row["error"] = "no Lean declaration found in output"
    return row


def write_row(arm: str, row: dict) -> None:
    p = out_path(arm, row["task_id"])
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(row, ensure_ascii=False, indent=2) + "\n")
    tmp.rename(p)


def load_tasks(path: Path) -> list[dict]:
    tasks = []
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            r = json.loads(line)
            if "_meta" in r:
                continue
            tasks.append(r)
    return tasks


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--arm", choices=list(ARM_CONFIG), required=True)
    ap.add_argument("--model", default=DEFAULT_MODEL)
    ap.add_argument("--tasks", type=Path, default=BRIDGE_TASKS)
    ap.add_argument("--split", choices=["eval", "dev", "all"], default="eval")
    ap.add_argument("--limit", type=int, default=0,
                    help="run only the first N tasks (id order; 0 = all)")
    ap.add_argument("--concurrency", type=int, default=2)
    ap.add_argument("--resume", action="store_true",
                    help="skip task ids that already have a non-error run file")
    ap.add_argument("--max-turns", type=int, default=30,
                    help="tool-turn budget stated in the prompt + recorded per row "
                         "(the installed claude CLI has no hard turn cap; the wall "
                         "clock is the runaway bound)")
    ap.add_argument("--timeout", type=int, default=600, help="seconds per task (10 min)")
    ap.add_argument("--claude-bin", default="claude")
    ap.add_argument("--dry-run", action="store_true",
                    help="print the resolved command + prompt for the first task, exit")
    args = ap.parse_args()

    if not args.dry_run and shutil.which(args.claude_bin) is None:
        print(f"error: claude CLI not found ({args.claude_bin!r})", file=sys.stderr)
        return 2

    if args.tasks.exists():
        tasks = [t for t in load_tasks(args.tasks)
                 if args.split == "all" or t.get("split") == args.split]
        tasks.sort(key=lambda t: t["id"])
    elif args.dry_run:
        print(f"note: {args.tasks} not found yet — using the built-in SAMPLE task "
              "for --dry-run only.", file=sys.stderr)
        tasks = [SAMPLE_TASK]
    else:
        print(f"error: task file not found: {args.tasks}\n"
              "(bench/data/bridge_tasks.jsonl is built separately; --dry-run works "
              "without it.)", file=sys.stderr)
        return 2
    if args.limit:
        tasks = tasks[: args.limit]
    if not tasks:
        print("no tasks selected", file=sys.stderr)
        return 1

    mcp_config = resolve_arm_config(args.arm)
    mcp_url = None
    if args.arm == "D" and mcp_config is not None:
        mcp_url = json.loads(mcp_config.read_text())["mcpServers"]["wikibrain"]["url"]

    if args.dry_run:
        t = tasks[0]
        print(f"arm {args.arm}: would run {len(tasks)} task(s) -> {RUNS_DIR / args.arm}/")
        print(f"mcp-config: {mcp_config}")
        print(f"allowedTools: {','.join(ARM_TOOLS[args.arm]) or '(none)'}")
        if args.arm == "D":
            print(f"preflight: would POST JSON-RPC initialize to {mcp_url} "
                  f"({PREFLIGHT_TIMEOUT_S}s; aborts run on failure) — SKIPPED in --dry-run")
        print("cmd:", json.dumps(build_cmd(args, "<PROMPT>", mcp_config)))
        print("--- prompt ---")
        print(build_prompt(t, args.max_turns))
        return 0

    env = dict(os.environ)
    # Max-auth gotcha (CLAUDE.md) + endpoint hygiene: a parent Claude Code session
    # exports ANTHROPIC_BASE_URL/USE_*_OAUTH, which sends the child CLI's valid
    # production token to the wrong endpoint (a misleading 401).
    for k in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL",
              "USE_STAGING_OAUTH", "USE_LOCAL_OAUTH", "CLAUDE_CODE_OAUTH_SCOPES"):
        env.pop(k, None)
    workdir = Path(tempfile.mkdtemp(prefix="bridgebench-"))
    try:
        done: set[str] = set()
        if args.resume:
            for t in tasks:
                p = out_path(args.arm, t["id"])
                if p.exists():
                    try:
                        if not json.loads(p.read_text()).get("error"):
                            done.add(t["id"])
                    except (json.JSONDecodeError, OSError):
                        pass
        todo = [t for t in tasks if t["id"] not in done]
        if not todo:
            print(f"nothing to do: all {len(tasks)} tasks already have run files.",
                  file=sys.stderr)
            return 0

        if args.arm == "D":
            print(f"preflight: JSON-RPC initialize -> {mcp_url}", file=sys.stderr)
            err = preflight_wikibrain(mcp_url)
            if err:
                bar = "=" * 72
                print(f"{bar}\nPREFLIGHT FAILED — Wikibrain MCP at {mcp_url}\n  {err}\n"
                      "Aborting: the claude CLI degrades SILENTLY to no-tools when the "
                      f"server is unreachable, which would corrupt arm D.\n{bar}",
                      file=sys.stderr)
                return 2
            print("preflight OK", file=sys.stderr)

        print(f"[arm {args.arm}/{args.model}] {len(todo)} tasks "
              f"({len(done)} resumed) -> {RUNS_DIR / args.arm}/", file=sys.stderr)
        print(f"cwd for CLI children: {workdir}", file=sys.stderr)
        lock = threading.Lock()
        n_done = n_err = n_auth = 0
        tool_seen = False
        with ThreadPoolExecutor(args.concurrency) as ex:
            futs = {ex.submit(run_one, t, args, mcp_config, env, workdir): t
                    for t in todo}
            for fut in as_completed(futs):
                row = fut.result()
                with lock:
                    write_row(args.arm, row)
                    n_done += 1
                    if row.get("error"):
                        n_err += 1
                        if re.search(r"authenticat|OAuth|api_status=401",
                                     row["error"], re.IGNORECASE):
                            n_auth += 1
                    if (row.get("transcript_stats") or {}).get("tool_calls_by_name"):
                        tool_seen = True
                    tc = sum((row.get("transcript_stats") or {})
                             .get("tool_calls_by_name", {}).values())
                    print(f"  [{n_done}/{len(todo)}] {row['task_id']} {row['wall_s']}s "
                          f"tools={tc}"
                          + (f"  ERROR: {row['error']}" if row.get("error") else ""),
                          file=sys.stderr)
        print(f"done: {n_done} rows, {n_err} errors -> {RUNS_DIR / args.arm}/",
              file=sys.stderr)
        if n_auth:
            print(f"NOTE: {n_auth}/{n_done} failures are Max-auth (OAuth token "
                  "expired). Re-authenticate by running `claude` interactively, then "
                  "re-run with --resume (the error rows are not skipped).",
                  file=sys.stderr)
        # Degradation canary: a tooled arm that made ZERO tool calls across every
        # task almost certainly lost its MCP server mid-run (silent no-tools).
        if args.arm != "A" and not tool_seen and n_done:
            print(f"WARNING: arm {args.arm} made 0 tool calls across all {n_done} "
                  "tasks — the MCP server may have dropped. Results are suspect.",
                  file=sys.stderr)
        return 0 if n_err == 0 else 1
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
