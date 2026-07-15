#!/usr/bin/env python3
"""Run one Wikibrain benchmark arm by shelling the claude CLI per task.

Arms (same model, same prompt — the ONLY difference is tool availability):
  no_tools  — model alone: built-in tools disallowed, no MCP servers.
  wikibrain — same, plus the Wikibrain MCP server (mcp__wikibrain__* cell tools).

The claude CLI runs on Max auth: ANTHROPIC_API_KEY is REMOVED from the child
environment (CLAUDE.md Max-auth gotcha — a set key makes calls fail silently).
Both arms pass --strict-mcp-config so the user's own MCP servers never leak in,
and run from a freshly-created empty temp dir OUTSIDE the repo (tempfile.mkdtemp)
so repo CLAUDE.md / .claude hooks+skills / cwd-keyed auto-memory cannot
contaminate either arm and file tools cannot see repo gold.

The wikibrain arm PREFLIGHTS the MCP server (JSON-RPC initialize over HTTP,
15s timeout) before any task runs and aborts loudly (exit 2) on failure — the
claude CLI otherwise degrades SILENTLY to a no-tools run when the server is
unreachable. A post-run canary warns if every completed wikibrain task
finished in <=1 turn (i.e. zero tool calls — mid-run degradation).

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
import ssl
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from tasklib import BENCH, DATA_DIR, TASKS_PATH, build_prompt, load_tasks, parse_answer  # noqa: E402

MCP_CONFIG_TEMPLATE = BENCH / "mcp-config.json"
DEFAULT_MODEL = "claude-haiku-4-5-20251001"

# The seven Wikibrain MCP tools (BRAIN v3 — docs/BRAIN-API.md) + the
# server-level rule as a catch-all if the tool list grows. `brain_cell`
# replaced brain_node + brain_unit at the cell cut: v3 has no particle nodes,
# and the unit card became the cell card. The v2 names still dispatch as
# aliases, so listing them costs nothing and keeps an older resumed run's
# transcripts replayable.
WIKIBRAIN_TOOLS = ["mcp__wikibrain"] + [
    f"mcp__wikibrain__{t}" for t in (
        "brain_search", "brain_cell", "brain_transfer", "brain_neighborhood",
        "brain_snippets", "brain_filter", "decl_exists",
        "brain_unit", "brain_node")  # v2 aliases of brain_cell
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


PREFLIGHT_TIMEOUT_S = 15


def _preflight_ssl_context() -> ssl.SSLContext:
    """Default context + certifi roots when available — the python.org macOS
    builds ship an empty OpenSSL store, which would false-abort the preflight."""
    ctx = ssl.create_default_context()
    try:
        import certifi
        ctx.load_verify_locations(certifi.where())
    except (ImportError, ssl.SSLError):
        pass
    return ctx


def preflight_wikibrain(url: str, timeout: float = PREFLIGHT_TIMEOUT_S) -> str | None:
    """POST a JSON-RPC initialize to the MCP URL; None on success, else an error
    string. Run BEFORE any wikibrain task: the claude CLI degrades silently to a
    no-tools run when the MCP server is unreachable."""
    body = json.dumps({
        "jsonrpc": "2.0", "id": 1, "method": "initialize",
        "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                   "clientInfo": {"name": "wikibench-preflight", "version": "1.0"}},
    }).encode()
    req = urllib.request.Request(url, data=body, method="POST", headers={
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
        # Cloudflare 403s the default Python-urllib UA before the Worker runs.
        "User-Agent": "wikibench-preflight/1.0",
    })
    try:
        with urllib.request.urlopen(req, timeout=timeout,
                                    context=_preflight_ssl_context()) as resp:
            status = resp.status
            ctype = resp.headers.get("Content-Type") or ""
            raw = resp.read(1 << 16).decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return f"HTTP {e.code} {e.reason}"
    except (urllib.error.URLError, OSError) as e:
        hint = ("  (local Python is missing root certs — `pip install certifi` "
                "or run Install Certificates.command)"
                if "CERTIFICATE_VERIFY_FAILED" in str(e) else "")
        return f"cannot reach server: {e}{hint}"
    if status != 200:
        return f"HTTP {status} (expected 200)"
    # Streamable-HTTP servers may answer as SSE; plain JSON otherwise.
    payload = None
    if "text/event-stream" in ctype:
        for line in raw.splitlines():
            if line.startswith("data:"):
                try:
                    payload = json.loads(line[len("data:"):].strip())
                    break
                except json.JSONDecodeError:
                    continue
    else:
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            payload = None
    if not (isinstance(payload, dict) and payload.get("jsonrpc") == "2.0"
            and "result" in payload):
        return (f"invalid JSON-RPC initialize response "
                f"(Content-Type {ctype!r}): {raw[:300]!r}")
    return None


def build_cmd(args: argparse.Namespace, prompt: str, mcp_config: Path | None) -> list[str]:
    # NB: no turn-limit flag — the installed claude CLI has none; --timeout
    # (wall clock per task) is the runaway bound.
    cmd = [args.claude_bin, "-p", prompt,
           "--output-format", "json",
           "--model", args.model,
           "--strict-mcp-config",
           "--disallowedTools", ",".join(DISALLOWED_TOOLS)]
    if args.arm == "no_tools":
        # Allowlist-only: `--tools ""` disables the ENTIRE built-in tool set
        # (claude CLI >= 2.1.x) — stronger than the deny-list above, which can
        # never be exhaustively closed. Both are kept (belt-and-suspenders),
        # plus the empty out-of-repo cwd (see main).
        cmd += ["--tools", ""]
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
    mcp_url = (json.loads(mcp_config.read_text())["mcpServers"]["wikibrain"]["url"]
               if mcp_config else None)

    env = dict(os.environ)
    env.pop("ANTHROPIC_API_KEY", None)  # Max-auth gotcha (CLAUDE.md)
    # Fresh empty cwd OUTSIDE the repo: the claude CLI discovers CLAUDE.md by
    # walking up from cwd, loads .claude/ hooks+skills+settings from cwd, and
    # keys the user-level auto-memory (~/.claude/projects/<mangled-cwd>) by cwd
    # — running inside the repo would contaminate BOTH arms (README "Isolation").
    workdir = Path(tempfile.mkdtemp(prefix="wikibench-"))
    try:
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
            print(f"cwd: {workdir} (fresh empty temp dir outside the repo; "
                  "removed on exit)")
            if args.arm == "wikibrain":
                print(f"preflight: would POST JSON-RPC initialize to {mcp_url} "
                      f"({PREFLIGHT_TIMEOUT_S}s timeout; non-200/invalid "
                      "JSON-RPC aborts the run, exit 2) — SKIPPED in --dry-run")
            print("cmd:", json.dumps(build_cmd(args, "<PROMPT>", mcp_config)))
            print("--- first prompt ---")
            print(build_prompt(t))
            return 0

        if args.arm == "wikibrain":
            print(f"preflight: JSON-RPC initialize -> {mcp_url}", file=sys.stderr)
            err = preflight_wikibrain(mcp_url)
            if err:
                bar = "=" * 72
                print(f"{bar}\nPREFLIGHT FAILED — Wikibrain MCP server at "
                      f"{mcp_url}\n  {err}\n"
                      "Aborting before any task runs: the claude CLI degrades "
                      "SILENTLY to a\nno-tools run when the MCP server is "
                      f"unreachable, which would corrupt the\nwikibrain arm.\n{bar}",
                      file=sys.stderr)
                return 2
            print("preflight OK", file=sys.stderr)

        print(f"[{args.arm}/{args.model}] {len(todo)} tasks "
              f"({len(done)} resumed) -> {out_path}", file=sys.stderr)
        print(f"cwd for CLI children: {workdir}", file=sys.stderr)
        lock = threading.Lock()
        n_done = n_err = 0
        completed_turns: list[int | None] = []  # n_turns of non-error rows
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
                    else:
                        completed_turns.append(row.get("n_turns"))
                    print(f"  [{n_done}/{len(todo)}] {row['task_id']} "
                          f"{row['latency_s']}s"
                          + (f"  ERROR: {row['error']}" if row.get("error") else ""),
                          file=sys.stderr)
        print(f"done: {n_done} rows, {n_err} errors -> {out_path}", file=sys.stderr)
        # Degradation canary: preflight only checks the server at START of run.
        # num_turns <= 1 means the model never called a tool; if EVERY completed
        # wikibrain task looks like that, the arm almost certainly degraded.
        if (args.arm == "wikibrain" and completed_turns
                and all(t is None or t <= 1 for t in completed_turns)):
            print("WARNING: every completed wikibrain task finished in <=1 turn "
                  "(0 tool calls).\nThe MCP server likely dropped mid-run and the "
                  "arm degraded to no_tools.\nTreat these results as suspect; "
                  "check the server and re-run.", file=sys.stderr)
        return 0 if n_err == 0 else 1
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
