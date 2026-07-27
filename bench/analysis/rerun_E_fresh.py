#!/usr/bin/env python3
"""Rerun arm E fresh-set rows via bench/run_bridge.py with MATHLIB_ROOT pinned.

Usage: rerun_E_fresh.py <tasks.jsonl> [concurrency]

- Monkeypatches run_bridge.MATHLIB_ROOT to the 61a5e4f338 pinned tree.
- Wraps run_bridge.parse_stream with a silent-MCP-degradation detector: the CLI
  stream-json init event lists mcp_servers status; if wiki+formal are not both
  "connected" the row is forced to an error (so --resume retries it) instead of
  being recorded as a fake tool-less success. Root cause: concurrent cold-start
  race observed 2026-07-27 (first wave of CLI children got 0 tools).
- Sets MCP_TIMEOUT=60000 to give stdio servers headroom.
- Everything else (model, max_turns=30, timeout=600, prompt, arm config, env
  scrubbing) is run_bridge's own July-19 code path, unchanged.
"""
import json
import os
import sys
from pathlib import Path

BENCH = Path("/Users/jack/Desktop/LEAN/WikiLean/bench")
PIN_MATHLIB = ("/private/tmp/claude-501/-Users-jack-Desktop-LEAN-WikiLean/"
               "0b16d2c8-53d8-49d0-8e6e-c03de5fb2eff/scratchpad/mathlib4-pin/Mathlib")
REQUIRED_SERVERS = {"wiki", "formal"}

sys.path.insert(0, str(BENCH))
import run_bridge  # noqa: E402

assert Path(PIN_MATHLIB).is_dir(), f"pinned Mathlib missing: {PIN_MATHLIB}"
run_bridge.MATHLIB_ROOT = PIN_MATHLIB
os.environ["MCP_TIMEOUT"] = "60000"

_orig_parse_stream = run_bridge.parse_stream


def parse_stream_with_mcp_check(stdout: str) -> dict:
    stats = _orig_parse_stream(stdout)
    status = None  # None = no init event seen
    for line in stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            continue
        if ev.get("type") == "system" and ev.get("subtype") == "init":
            status = {s.get("name"): s.get("status")
                      for s in ev.get("mcp_servers") or []}
            break
    connected = {n for n, st in (status or {}).items() if st == "connected"}
    if not REQUIRED_SERVERS <= connected:
        detail = json.dumps(status) if status is not None else "no init event"
        print(f"  MCP-DEGRADED: servers={detail} -> forcing error row",
              file=sys.stderr)
        stats["is_error"] = True
        stats["subtype"] = f"mcp_degraded servers={detail}"
    return stats


run_bridge.parse_stream = parse_stream_with_mcp_check

tasks = sys.argv[1]
conc = sys.argv[2] if len(sys.argv) > 2 else "3"
sys.argv = ["run_bridge.py", "--arm", "E", "--tasks", tasks, "--split", "all",
            "--resume", "--model", "claude-haiku-4-5-20251001",
            "--concurrency", conc]
sys.exit(run_bridge.main())
