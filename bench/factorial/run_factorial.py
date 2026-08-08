#!/usr/bin/env python3
"""Bridge 2x2 factorial runner (join x existence-verifier).

Preregistration: docs/research/BRIDGE-FACTORIAL.md (commit 3658bd58 — committed
BEFORE this harness was built). This runner executes the 400 (task, arm) pairs
of the factorial: 100 fresh tasks (bench/data/fresh_tasks.jsonl) x 4 arms, in
ONE seeded shuffled interleaved order (seed 20260803, NO arm blocks).

Arms (docs/research/BRIDGE-FACTORIAL.md §2; tool manifests are the ONLY
between-arm difference — identical model, prompt, budgets):

  Ep  join-, ver-   wiki+formal stdio (arm-E toolset exactly)
  X   join-, ver+   Ep + wikibrain with ONLY decl_exists model-visible
  J   join+, ver-   wikibrain minus decl_exists
  Dp  join+, ver+   full wikibrain (arm-D exactly)

Execution-fidelity machinery (v3 report §3.4 lessons, all mechanical):
  - hard turn cap 30 via the CLI's (undocumented but verified) --max-turns
  - per-row attach-signature + EXACT tool-manifest validation from the
    stream-json system/init event; condemned rows auto-retry (5 attempts,
    then a loud halt — never a silent drop)
  - per-row condition hash (arm + model + tools + resolved config + pins)
  - 429/usage-limit is never a terminal row: global hold (parses "resets
    7:30am"-style fragments like site/ops/retry-lib.sh; else backoff) + retry
  - Max-auth env scrub; empty temp cwd outside the repo; --strict-mcp-config;
    --tools "" everywhere; excluded MCP tools deny-listed (removes them from
    the model-visible manifest — verified against CLI 2.1.153)
  - full raw stream-json transcript gzipped per row (bench/v2 convention)

Rows -> bench/data/runs_factorial/{Ep,X,J,Dp}/fresh_XXX.json (+ .stream.jsonl.gz)
Dry-run (--dryrun --limit 2) -> bench/data/runs_factorial_dryrun/... (deleted
after the Stage-3 gate passes).

Usage:
  python3 bench/factorial/run_factorial.py --probe          # manifest probe only
  python3 bench/factorial/run_factorial.py --dryrun --limit 2
  python3 bench/factorial/run_factorial.py                  # the full 400
  python3 bench/factorial/run_factorial.py --status         # disk-state summary
"""
from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

HERE = Path(__file__).resolve().parent
BENCH = HERE.parent
REPO = BENCH.parent
sys.path.insert(0, str(BENCH))
import run_bridge  # noqa: E402  — build_prompt / extract_lean reused verbatim
from run_benchmark import DISALLOWED_TOOLS, preflight_wikibrain  # noqa: E402

# ---------------------------------------------------------------------------- #
# Constants (all preregistered — BRIDGE-FACTORIAL.md §§2-4)                     #
# ---------------------------------------------------------------------------- #
SEED = 20260803
MODEL = "claude-haiku-4-5-20251001"
MAX_TURNS = 30
TIMEOUT_S = 600
CONCURRENCY = 4
STAGGER_S = 5.0
MAX_ATTEMPTS = 5          # infrastructure retries per pair (429 never counts)
RETRY_BACKOFF_S = 20.0
LIMIT_BACKOFF_START_S = 60.0
LIMIT_BACKOFF_CAP_S = 3600.0
TASKS_FILE = BENCH / "data" / "fresh_tasks.jsonl"
RUNS_DIR = BENCH / "data" / "runs_factorial"
DRYRUN_DIR = BENCH / "data" / "runs_factorial_dryrun"
MATHLIB_PIN_SHA = "61a5e4f338bfdddf2f6296402a49fe80f3b1a147"
PIN_ROOT = Path("/private/tmp/claude-501/-Users-jack-Desktop-LEAN-WikiLean/"
                "0b16d2c8-53d8-49d0-8e6e-c03de5fb2eff/scratchpad/mathlib4-pin/Mathlib")
LIVE_CHECKOUT = Path("/Users/jack/Desktop/LEAN/mathlib4")  # read-only, sentinel check
TRACE_CAP, INPUT_TRUNC, RESULT_TRUNC = 400, 2000, 4000     # v2 max-telemetry caps

WIKI_TOOLS = ["mcp__wiki__wiki_search", "mcp__wiki__wiki_get", "mcp__wiki__nlab_search"]
FORMAL_TOOLS = ["mcp__formal__loogle", "mcp__formal__decl_grep", "mcp__formal__decl_read"]
JOIN_TOOLS = [f"mcp__wikibrain__{t}" for t in (
    "brain_bridge", "brain_search", "brain_cell", "brain_transfer",
    "brain_neighborhood", "brain_snippets", "brain_filter")]
VERIFIER_TOOL = "mcp__wikibrain__decl_exists"
ALIAS_TOOL = "mcp__wikibrain__brain_unit"  # dispatch alias, never listed; denied defensively

ARMS: dict[str, dict] = {
    "Ep": {"cfg": "mcp-Ep.json", "cell": {"join": False, "verifier": False},
           "allowed": WIKI_TOOLS + FORMAL_TOOLS,
           "deny_extra": [],
           "manifest": sorted(WIKI_TOOLS + FORMAL_TOOLS),
           "servers": {"wiki", "formal"}},
    "X":  {"cfg": "mcp-X.json", "cell": {"join": False, "verifier": True},
           "allowed": WIKI_TOOLS + FORMAL_TOOLS + [VERIFIER_TOOL],
           "deny_extra": JOIN_TOOLS + [ALIAS_TOOL],
           "manifest": sorted(WIKI_TOOLS + FORMAL_TOOLS + [VERIFIER_TOOL]),
           "servers": {"wiki", "formal", "wikibrain"}},
    "J":  {"cfg": "mcp-J.json", "cell": {"join": True, "verifier": False},
           "allowed": JOIN_TOOLS,
           "deny_extra": [VERIFIER_TOOL, ALIAS_TOOL],
           "manifest": sorted(JOIN_TOOLS),
           "servers": {"wikibrain"}},
    "Dp": {"cfg": "mcp-Dp.json", "cell": {"join": True, "verifier": True},
           "allowed": JOIN_TOOLS + [VERIFIER_TOOL],
           "deny_extra": [],
           "manifest": sorted(JOIN_TOOLS + [VERIFIER_TOOL]),
           "servers": {"wikibrain"}},
}
ARM_ORDER = ["Ep", "X", "J", "Dp"]  # enumeration order inside each task (prereg §4.1)

LIMIT_RE = re.compile(r"429|usage limit|session limit|hit your limit|rate.?limit"
                      r"|overloaded|rate_limited|resets\s+\d", re.IGNORECASE)
AUTH_RE = re.compile(r"authenticat|OAuth|api_status=401|invalid.?api.?key", re.IGNORECASE)
RESET_RE = re.compile(r"resets?\s+(?:at\s+)?(\d{1,2})(?::(\d{2}))?\s*(am|pm)", re.IGNORECASE)


# ---------------------------------------------------------------------------- #
# Pin verification — the extraction must BE the 61a5e4f338 tree.                #
# ---------------------------------------------------------------------------- #
SENTINELS = ["Mathlib/Algebra/Group/Defs.lean",
             "Mathlib/Topology/Basic.lean",
             "Mathlib/Order/Basic.lean"]


def verify_pin() -> None:
    if not PIN_ROOT.is_dir():
        sys.exit(f"FATAL: pinned Mathlib extraction missing: {PIN_ROOT}\n"
                 f"Recreate: git -C {LIVE_CHECKOUT} archive {MATHLIB_PIN_SHA} Mathlib "
                 f"| tar -x -C {PIN_ROOT.parent}")
    for rel in SENTINELS:
        want = subprocess.run(
            ["git", "-C", str(LIVE_CHECKOUT), "show", f"{MATHLIB_PIN_SHA}:{rel}"],
            capture_output=True).stdout
        got = (PIN_ROOT.parent / rel).read_bytes()
        if hashlib.sha256(want).hexdigest() != hashlib.sha256(got).hexdigest():
            sys.exit(f"FATAL: pinned tree sentinel mismatch: {rel} is not the "
                     f"{MATHLIB_PIN_SHA[:10]} content. Re-extract the pin.")


# ---------------------------------------------------------------------------- #
# Config resolution (run_bridge's, pointed at the factorial configs + pin).     #
# ---------------------------------------------------------------------------- #
def resolve_cfg(arm: str) -> Path:
    cfg = json.loads((BENCH / "arms" / ARMS[arm]["cfg"]).read_text())
    cfg.pop("_arm", None)
    url_override = os.environ.get("WIKIBRAIN_MCP_URL")
    for _, spec in cfg.get("mcpServers", {}).items():
        if spec.get("type") == "http":
            if url_override:
                spec["url"] = url_override
        else:
            spec["args"] = [str((REPO / a).resolve()) if a.endswith(".py") else a
                            for a in spec.get("args", [])]
            env = dict(spec.get("env") or {})
            env["MATHLIB_ROOT"] = str(PIN_ROOT)
            claude_exec = os.environ.get("CLAUDE_CODE_EXECPATH") or shutil.which("claude")
            if claude_exec:
                env.setdefault("CLAUDE_CODE_EXECPATH", claude_exec)
            spec["env"] = env
    out = BENCH / "data" / f".mcp-bridge-factorial-{arm}.resolved.json"
    out.write_text(json.dumps(cfg, indent=2) + "\n")
    return out


def scrub_env() -> dict:
    env = dict(os.environ)
    for k in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL",
              "USE_STAGING_OAUTH", "USE_LOCAL_OAUTH", "CLAUDE_CODE_OAUTH_SCOPES"):
        env.pop(k, None)
    env.setdefault("MCP_TIMEOUT", "120000")
    return env


def cli_version() -> str:
    try:
        return subprocess.run(["claude", "-v"], capture_output=True, text=True,
                              timeout=30).stdout.strip()
    except (subprocess.SubprocessError, OSError):
        return "unknown"


def condition_hash(arm: str, resolved_cfg: Path, cliver: str) -> str:
    a = ARMS[arm]
    payload = {
        "arm": arm, "model": MODEL, "max_turns": MAX_TURNS,
        "allowed_tools": sorted(a["allowed"]),
        "disallowed_tools": sorted(DISALLOWED_TOOLS + a["deny_extra"]),
        "expected_manifest": a["manifest"],
        "mcp_config": json.loads(resolved_cfg.read_text()),
        "mathlib_pin": MATHLIB_PIN_SHA,
        "tasks_sha256": hashlib.sha256(TASKS_FILE.read_bytes()).hexdigest(),
        "prompt_template_sha256": hashlib.sha256(
            run_bridge.build_prompt(
                {"informal_statement": "\x00STATEMENT\x00"}, MAX_TURNS).encode()
        ).hexdigest(),
        "cli_version": cliver,
    }
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def build_cmd(arm: str, prompt: str, resolved_cfg: Path) -> list[str]:
    a = ARMS[arm]
    return ["claude", "-p", prompt,
            "--output-format", "stream-json", "--verbose",
            "--model", MODEL,
            "--max-turns", str(MAX_TURNS),
            "--strict-mcp-config",
            "--tools", "",
            "--mcp-config", str(resolved_cfg),
            "--allowedTools", ",".join(a["allowed"]),
            "--disallowedTools", ",".join(DISALLOWED_TOOLS + a["deny_extra"])]


# ---------------------------------------------------------------------------- #
# stream-json parsing — run_agent 834a130a's parse + assistant-text capture     #
# (cap fallback) + api_error_status (run_bridge).                               #
# ---------------------------------------------------------------------------- #
def parse_stream(stdout: str) -> dict:
    result_text, subtype, is_error, api_err = "", None, None, None
    turns = cost = tin = tout = None
    init_tools: list[str] | None = None
    mcp_init: list[list[str]] | None = None
    tool_calls: dict[str, int] = {}
    trace: list[dict] = []
    by_id: dict[str, dict] = {}
    assistant_texts: list[str] = []
    # The --max-turns cap unit is assistant MESSAGES (dry-run finding: capped
    # rows have exactly 30 distinct assistant message ids; the result event's
    # num_turns counts tool-result user messages + 1 — a different unit).
    assistant_msg_ids: set[str] = set()
    for line in stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            continue
        et = ev.get("type")
        if et == "system" and ev.get("subtype") == "init":
            mcp_init = [[s.get("name", "?"), s.get("status", "?")]
                        for s in ev.get("mcp_servers") or []]
            init_tools = sorted(ev.get("tools") or [])
        elif et == "assistant":
            mid = (ev.get("message") or {}).get("id")
            if mid:
                assistant_msg_ids.add(mid)
            for blk in (ev.get("message") or {}).get("content", []) or []:
                if not isinstance(blk, dict):
                    continue
                if blk.get("type") == "text" and blk.get("text"):
                    assistant_texts.append(blk["text"])
                elif blk.get("type") == "tool_use":
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
            api_err = ev.get("api_error_status", api_err)
            turns = ev.get("num_turns", turns)
            cost = ev.get("total_cost_usd", cost)
            u = ev.get("usage") or {}
            tin, tout = u.get("input_tokens", tin), u.get("output_tokens", tout)
    return {"result_text": result_text, "subtype": subtype, "is_error": is_error,
            "api_error_status": api_err, "turns": turns, "cost_usd": cost,
            "tokens_in": tin, "tokens_out": tout, "tool_calls_by_name": tool_calls,
            "tool_trace": trace, "mcp_init": mcp_init, "init_tools": init_tools,
            "assistant_turns": len(assistant_msg_ids),
            "assistant_text": "\n\n".join(assistant_texts)}


# ---------------------------------------------------------------------------- #
# Global 429 hold + halt machinery                                              #
# ---------------------------------------------------------------------------- #
class Coordinator:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.hold_until = 0.0
        self.limit_backoff = LIMIT_BACKOFF_START_S
        self.halt = threading.Event()
        self.halt_reason = ""

    def wait_if_held(self) -> None:
        while not self.halt.is_set():
            with self.lock:
                remaining = self.hold_until - time.time()
            if remaining <= 0:
                return
            time.sleep(min(remaining, 10.0))

    def enter_hold(self, error_text: str) -> None:
        """Parse a Max reset time when present (retry-lib.sh convention: 'resets
        7:30am' +120s buffer), else exponential backoff. Global: all workers."""
        m = RESET_RE.search(error_text or "")
        wait: float
        if m:
            hh = int(m.group(1)) % 12 + (12 if m.group(3).lower() == "pm" else 0)
            mm = int(m.group(2) or 0)
            now = time.localtime()
            target = time.mktime((now.tm_year, now.tm_mon, now.tm_mday, hh, mm, 0,
                                  0, 0, -1))
            if target <= time.time():
                target += 86400.0
            wait = target - time.time() + 120.0
        else:
            with self.lock:
                wait = self.limit_backoff
                self.limit_backoff = min(self.limit_backoff * 2, LIMIT_BACKOFF_CAP_S)
        with self.lock:
            until = time.time() + wait
            if until > self.hold_until:
                self.hold_until = until
        print(f"  [limit] entering global hold for {int(wait)}s "
              f"({'parsed reset' if m else 'backoff'})", file=sys.stderr, flush=True)

    def reset_backoff(self) -> None:
        with self.lock:
            self.limit_backoff = LIMIT_BACKOFF_START_S

    def request_halt(self, reason: str) -> None:
        with self.lock:
            if not self.halt_reason:
                self.halt_reason = reason
        self.halt.set()


# ---------------------------------------------------------------------------- #
# One attempt of one (task, arm) pair                                           #
# ---------------------------------------------------------------------------- #
def attempt_pair(task: dict, arm: str, resolved_cfg: Path, cond_hash: str,
                 cliver: str, env: dict, workdir: Path, out_dir: Path,
                 attempt: int) -> tuple[str, dict]:
    """Returns (verdict, row): verdict in {'valid', 'condemned', 'limit', 'auth'}."""
    a = ARMS[arm]
    prompt = run_bridge.build_prompt(task, MAX_TURNS)
    cmd = build_cmd(arm, prompt, resolved_cfg)
    row: dict = {"task_id": task["id"], "arm": arm, "model": MODEL,
                 "max_turns": MAX_TURNS, "factorial_cell": a["cell"],
                 "condition_hash": cond_hash, "cli_version": cliver,
                 "attempt": attempt,
                 "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
    t0 = time.monotonic()
    timed_out = False
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True,
                              timeout=TIMEOUT_S, env=env, cwd=workdir)
        stdout = proc.stdout
    except subprocess.TimeoutExpired as e:
        stdout = (e.stdout or b"").decode() if isinstance(e.stdout, bytes) \
            else (e.stdout or "")
        timed_out = True
    row["wall_s"] = round(time.monotonic() - t0, 1)
    with gzip.open(out_dir / f"{task['id']}.stream.jsonl.gz", "wt") as f:
        f.write(stdout)
    st = parse_stream(stdout)
    row["transcript_stats"] = {
        "turns": st["turns"], "assistant_turns": st["assistant_turns"],
        "tool_calls_by_name": st["tool_calls_by_name"],
        "tokens_in": st["tokens_in"], "tokens_out": st["tokens_out"],
        "cost_usd": st["cost_usd"]}
    row["tool_trace"] = st["tool_trace"]
    row["mcp_init"] = st["mcp_init"]
    row["init_tools"] = st["init_tools"]

    err_blob = " ".join(filter(None, [
        st["result_text"] or "", str(st["subtype"] or ""),
        f"api_status={st['api_error_status']}" if st["api_error_status"] else ""]))

    # --- classification ladder (prereg §4) --------------------------------- #
    if timed_out:
        row["error"] = f"timeout after {TIMEOUT_S}s"
        return "condemned", row
    if LIMIT_RE.search(err_blob) and (st["is_error"] or
                                      st["subtype"] not in (None, "success",
                                                            "error_max_turns")):
        row["error"] = f"usage-limit: {err_blob[:300]}"
        return "limit", row
    if AUTH_RE.search(err_blob) and st["is_error"]:
        row["error"] = f"auth: {err_blob[:300]}"
        return "auth", row

    # Attach-signature + exact-manifest validation (strengthened 834a130a check)
    connected = {n for n, s in (st["mcp_init"] or []) if s == "connected"}
    if st["mcp_init"] is None:
        row["error"] = "no system/init event in stream"
        return "condemned", row
    if not a["servers"] <= connected:
        row["error"] = (f"mcp not attached at init: "
                        f"{json.dumps(st['mcp_init'])} (need {sorted(a['servers'])})")
        return "condemned", row
    if st["init_tools"] != a["manifest"]:
        row["error"] = (f"manifest mismatch at init: got {st['init_tools']} "
                        f"want {a['manifest']}")
        return "condemned", row
    row["attach_ok"] = True

    capped = st["subtype"] == "error_max_turns"
    row["capped"] = capped
    if st["is_error"] and not capped:
        row["error"] = f"CLI error (subtype={st['subtype']}): {err_blob[:300]}"
        return "condemned", row
    if not capped and not (st["result_text"] or "").strip():
        row["error"] = "empty result (0-token Max-auth symptom?)"
        return "condemned", row

    # Output extraction: result text; for capped rows, assistant-text fallback.
    out = run_bridge.extract_lean(st["result_text"])
    if out is None and capped:
        out = run_bridge.extract_lean(st["assistant_text"])
        row["extraction"] = "assistant_text_fallback" if out else "none"
    row["output_lean"] = out
    if out is None:
        row["no_lean"] = True  # VALID terminal row; scores as failure (prereg §5)
    return "valid", row


def write_row(out_dir: Path, row: dict) -> None:
    p = out_dir / f"{row['task_id']}.json"
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(row, ensure_ascii=False, indent=1) + "\n")
    tmp.rename(p)


# ---------------------------------------------------------------------------- #
# The preregistered order + worker loop                                         #
# ---------------------------------------------------------------------------- #
def load_tasks() -> list[dict]:
    tasks = []
    for line in TASKS_FILE.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        r = json.loads(line)
        if "_meta" not in r:
            tasks.append(r)
    tasks.sort(key=lambda t: t["id"])
    return tasks


def build_order(task_ids: list[str]) -> list[tuple[str, str]]:
    import random
    pairs = [(tid, arm) for tid in task_ids for arm in ARM_ORDER]
    random.Random(SEED).shuffle(pairs)
    return pairs


def is_terminal(out_root: Path, tid: str, arm: str) -> bool:
    p = out_root / arm / f"{tid}.json"
    if not p.exists():
        return False
    try:
        return not json.loads(p.read_text()).get("error")
    except (json.JSONDecodeError, OSError):
        return False


def worker(idx_iter, pairs, tasks_by_id, out_root, cfgs, hashes, cliver, env,
           workdir, coord: Coordinator, progress: dict, plock: threading.Lock,
           stagger_slot: int) -> None:
    if stagger_slot:
        time.sleep(STAGGER_S * stagger_slot)  # first-wave stagger (834a130a)
    while not coord.halt.is_set():
        try:
            i = next(idx_iter)
        except StopIteration:
            return
        tid, arm = pairs[i]
        out_dir = out_root / arm
        out_dir.mkdir(parents=True, exist_ok=True)
        attempts = 0
        while not coord.halt.is_set():
            coord.wait_if_held()
            if coord.halt.is_set():
                return
            attempts += 1
            verdict, row = attempt_pair(tasks_by_id[tid], arm, cfgs[arm],
                                        hashes[arm], cliver, env, workdir,
                                        out_dir, attempts)
            if verdict == "valid":
                write_row(out_dir, row)
                coord.reset_backoff()
                with plock:
                    progress["done"] += 1
                    n = progress["done"]
                tc = sum(row["transcript_stats"]["tool_calls_by_name"].values())
                print(f"  [{n}/{progress['total']}] {tid}/{arm} "
                      f"{row['wall_s']}s tools={tc}"
                      + (" CAPPED" if row.get("capped") else "")
                      + (" NO-LEAN" if row.get("no_lean") else ""),
                      file=sys.stderr, flush=True)
                break
            if verdict == "limit":
                write_row(out_dir, row)      # visible on disk, but not terminal
                with plock:
                    progress["limit_retries"] += 1
                coord.enter_hold(row.get("error", ""))
                attempts -= 1                # 429 never counts (prereg §4.5)
                continue
            if verdict == "auth":
                write_row(out_dir, row)
                coord.request_halt(f"AUTH failure on {tid}/{arm}: "
                                   f"{row.get('error')} — re-authenticate "
                                   "(`claude` interactively), then resume.")
                return
            # condemned — preserve the raw stream of the failed attempt
            # (the v3 convention: originals byte-preserved, §3.4)
            stream = out_dir / f"{tid}.stream.jsonl.gz"
            if stream.exists():
                stream.rename(out_dir / f"{tid}.condemned{attempts}.stream.jsonl.gz")
            write_row(out_dir, row)
            with plock:
                progress["condemned"] += 1
            print(f"  [condemned attempt {attempts}/{MAX_ATTEMPTS}] {tid}/{arm}: "
                  f"{row.get('error', '')[:140]}", file=sys.stderr, flush=True)
            if attempts >= MAX_ATTEMPTS:
                coord.request_halt(
                    f"{tid}/{arm} condemned {MAX_ATTEMPTS}x — investigate "
                    "before resuming (prereg §4.3: no silent drops).")
                return
            time.sleep(RETRY_BACKOFF_S)


# ---------------------------------------------------------------------------- #
# Probe: init-event manifest listing per arm (Stage-3 gate evidence)            #
# ---------------------------------------------------------------------------- #
def probe(env: dict, workdir: Path) -> int:
    cliver = cli_version()
    ok = True
    for arm in ARM_ORDER:
        cfg = resolve_cfg(arm)
        cmd = build_cmd(arm, "Reply with exactly: OK", cfg)
        cmd[cmd.index("--max-turns") + 1] = "1"
        got, servers = None, None
        for attempt in range(3):
            proc = subprocess.run(cmd, capture_output=True, text=True,
                                  timeout=180, env=env, cwd=workdir)
            st = parse_stream(proc.stdout)
            got, servers = st["init_tools"], st["mcp_init"]
            connected = {n for n, s in (servers or []) if s == "connected"}
            if ARMS[arm]["servers"] <= connected:
                break
            time.sleep(5)
        match = got == ARMS[arm]["manifest"]
        ok = ok and match
        print(f"[{arm}] servers={servers}\n  manifest={got}\n  "
              f"expected={ARMS[arm]['manifest']}\n  MATCH={match}")
    print(f"cli={cliver}  probe {'PASS' if ok else 'FAIL'}")
    return 0 if ok else 1


def status(out_root: Path) -> int:
    total_cost = 0.0
    for arm in ARM_ORDER:
        d = out_root / arm
        rows = sorted(d.glob("fresh_*.json")) if d.is_dir() else []
        term = err = capped = nolean = zerotool = 0
        for p in rows:
            r = json.loads(p.read_text())
            if r.get("error"):
                err += 1
                continue
            term += 1
            capped += bool(r.get("capped"))
            nolean += bool(r.get("no_lean"))
            zerotool += not any(k.startswith("mcp__") for k in
                                (r.get("transcript_stats") or {})
                                .get("tool_calls_by_name", {}))
            total_cost += (r.get("transcript_stats") or {}).get("cost_usd") or 0
        print(f"[{arm}] terminal={term} nonterminal_error={err} capped={capped} "
              f"no_lean={nolean} zero_tool={zerotool}")
    print(f"terminal-row cost total=${total_cost:.2f}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--dryrun", action="store_true",
                    help=f"write rows to {DRYRUN_DIR.name}/ instead")
    ap.add_argument("--limit", type=int, default=0,
                    help="first N tasks only (sorted ids; 0 = all 100)")
    ap.add_argument("--probe", action="store_true",
                    help="per-arm init-event manifest probe, no task rows")
    ap.add_argument("--status", action="store_true", help="disk-state summary")
    args = ap.parse_args()

    out_root = DRYRUN_DIR if args.dryrun else RUNS_DIR
    if args.status:
        return status(out_root)

    verify_pin()
    if shutil.which("claude") is None:
        sys.exit("FATAL: claude CLI not found")
    env = scrub_env()
    workdir = Path(tempfile.mkdtemp(prefix="factorial-"))
    try:
        if args.probe:
            return probe(env, workdir)

        # Wikibrain preflight (X/J/Dp): the CLI degrades silently otherwise.
        url = json.loads(resolve_cfg("Dp").read_text())["mcpServers"]["wikibrain"]["url"]
        err = preflight_wikibrain(url)
        if err:
            sys.exit(f"FATAL: wikibrain preflight failed ({url}): {err}")
        print(f"preflight OK ({url})", file=sys.stderr)

        tasks = load_tasks()
        if args.limit:
            tasks = tasks[: args.limit]
        tasks_by_id = {t["id"]: t for t in tasks}
        pairs = build_order(sorted(tasks_by_id))
        todo_idx = [i for i, (tid, arm) in enumerate(pairs)
                    if not is_terminal(out_root, tid, arm)]
        print(f"{len(pairs)} pairs in preregistered order (seed {SEED}); "
              f"{len(pairs) - len(todo_idx)} already terminal; {len(todo_idx)} to run "
              f"-> {out_root}/", file=sys.stderr)
        if not todo_idx:
            print("nothing to do", file=sys.stderr)
            return 0

        cliver = cli_version()
        cfgs = {arm: resolve_cfg(arm) for arm in ARM_ORDER}
        hashes = {arm: condition_hash(arm, cfgs[arm], cliver) for arm in ARM_ORDER}
        (out_root / "conditions.json").parent.mkdir(parents=True, exist_ok=True)
        (out_root / "conditions.json").write_text(json.dumps(
            {"seed": SEED, "model": MODEL, "max_turns": MAX_TURNS,
             "cli_version": cliver, "mathlib_pin": MATHLIB_PIN_SHA,
             "condition_hashes": hashes,
             "arms": {a: {"allowed": ARMS[a]["allowed"],
                          "deny_extra": ARMS[a]["deny_extra"],
                          "manifest": ARMS[a]["manifest"]} for a in ARM_ORDER}},
            indent=1) + "\n")

        coord = Coordinator()
        progress = {"done": len(pairs) - len(todo_idx), "total": len(pairs),
                    "condemned": 0, "limit_retries": 0}
        plock = threading.Lock()
        idx_iter = iter(todo_idx)
        it_lock = threading.Lock()

        def safe_iter():
            # NB: take the lock only around next() — yielding inside the
            # `with` would hold the lock across the whole pair execution and
            # silently serialize all workers (caught by the first dry-run).
            while True:
                with it_lock:
                    try:
                        i = next(idx_iter)
                    except StopIteration:
                        return
                yield i

        gens = [safe_iter() for _ in range(CONCURRENCY)]
        with ThreadPoolExecutor(CONCURRENCY) as ex:
            futs = [ex.submit(worker, gens[k], pairs, tasks_by_id, out_root,
                              cfgs, hashes, cliver, env, workdir, coord,
                              progress, plock, k)
                    for k in range(CONCURRENCY)]
            for f in futs:
                f.result()
        if coord.halt.is_set():
            bar = "=" * 72
            print(f"{bar}\nHALTED: {coord.halt_reason}\n{bar}", file=sys.stderr)
            return 2
        print(f"complete: {progress['done']}/{progress['total']} terminal; "
              f"condemned-retries {progress['condemned']}, "
              f"limit-retries {progress['limit_retries']}", file=sys.stderr)
        return 0
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
