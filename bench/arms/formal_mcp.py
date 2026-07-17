#!/usr/bin/env python3
"""formal_mcp — arm C (formal-only) stdio MCP server for the Bridge Experiment.

Gives an agent the LeanSearch-class formal status quo and NOTHING informal: a
pattern search over Mathlib (Loogle), a name/regex grep over the pinned Mathlib
checkout, and a source-window reader. There is deliberately no Wikipedia, no
Wikidata QID, no nLab, no concept layer and no concept↔decl mapping anywhere in
this file — arm C isolates "search the formal library directly"
(docs/research/BRIDGE-EXPERIMENT.md).

Tools:
  loogle(q)                 Loogle pattern/name/type search over Mathlib
  decl_grep(pattern, limit) ripgrep decl headers over the Mathlib checkout (file:line)
  decl_read(file, line, n)  read an n-line window of a Mathlib source file

The Mathlib checkout is READ-ONLY: decl_grep/decl_read never write, and every
path is confined to MATHLIB_ROOT (…/mathlib4/Mathlib) — a traversal outside it is
refused.

Protocol: JSON-RPC 2.0 over stdio, newline-delimited (the MCP stdio transport).
Handles initialize / tools/list / tools/call / ping; notifications get no reply.
Every tool response is size-capped and every tool error is returned AS a tool
result (isError:true), never as a crash or a protocol error.

Stdlib only (urllib + subprocess) — no third-party deps, no venv.

Self-test:  python3 formal_mcp.py --selftest   (exercises the 3 tools + dispatch)
"""
from __future__ import annotations

import json
import os
import re
import shutil
import ssl
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

SERVER_NAME = "formal"
SERVER_VERSION = "1.0.0"
DEFAULT_PROTOCOL = "2024-11-05"
UA = ("WikiLean-bridge-bench/1.0 "
      "(https://github.com/Deicyde; wikilean@jackmccarthy.org)")

MATHLIB_ROOT = Path(os.environ.get(
    "MATHLIB_ROOT", "/Users/jack/Desktop/LEAN/mathlib4/Mathlib")).resolve()
LOOGLE_URL = "https://loogle.lean-lang.org/json"

LOOGLE_HITS = 12       # loogle hits returned
GREP_LIMIT = 20        # decl_grep default hit cap
GREP_MAX = 60          # decl_grep hard cap
READ_MAX = 120         # decl_read hard line cap
TEXT_CAP = 6000        # per-response text cap
HTTP_TIMEOUT = 30

# decl keyword heads for grep header framing
_DECL_HEADS = ("theorem", "lemma", "def", "abbrev", "instance", "structure",
               "class", "inductive", "example", "noncomputable")


# --------------------------------------------------------------------------- #
# HTTP (stdlib urllib + certifi fallback)                                       #
# --------------------------------------------------------------------------- #
def _ssl_context() -> ssl.SSLContext:
    ctx = ssl.create_default_context()
    try:
        import certifi
        ctx.load_verify_locations(certifi.where())
    except (ImportError, ssl.SSLError):
        pass
    return ctx


def http_get(url: str, params: dict | None = None, timeout: int = HTTP_TIMEOUT) -> str:
    if params:
        url = url + "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"User-Agent": UA,
                                               "Accept": "application/json"})
    last = None
    for _ in range(2):
        try:
            with urllib.request.urlopen(req, timeout=timeout,
                                        context=_ssl_context()) as resp:
                return resp.read().decode("utf-8", "replace")
        except (urllib.error.URLError, OSError, ssl.SSLError) as e:  # noqa: PERF203
            last = e
    raise RuntimeError(f"GET failed: {url[:120]} :: {last}")


def cap(s: str, n: int = TEXT_CAP) -> str:
    s = s or ""
    return s if len(s) <= n else s[:n].rstrip() + f"\n…[truncated at {n} chars]"


# --------------------------------------------------------------------------- #
# ripgrep resolution: real rg binary → claude-as-rg shim → pure-python fallback #
# --------------------------------------------------------------------------- #
def _rg_invocation() -> tuple[list[str], str | None] | None:
    """Return (argv_prefix, executable) for a working ripgrep, or None.

    On this machine `rg` is a shell function that execs the claude binary with
    argv0=rg (Claude Code's bundled ripgrep); a plain subprocess doesn't inherit
    the function, so we detect a real rg binary first, then fall back to invoking
    the claude binary AS rg via the argv0 trick (executable=claude_bin)."""
    real = shutil.which("rg")
    if real:
        return ([real], None)
    claude = os.environ.get("CLAUDE_CODE_EXECPATH") or shutil.which("claude")
    if claude and os.path.exists(claude):
        return (["rg"], claude)  # argv0="rg", executable=claude
    return None


def _run_rg(pattern: str, limit: int) -> list[str] | None:
    inv = _rg_invocation()
    if inv is None:
        return None
    argv0, executable = inv
    cmd = argv0 + ["-n", "--no-heading", "-g", "*.lean",
                   "--max-count", str(max(1, limit)), pattern, str(MATHLIB_ROOT)]
    try:
        proc = subprocess.run(cmd, executable=executable, capture_output=True,
                              text=True, timeout=40)
    except (subprocess.TimeoutExpired, OSError):
        return None
    if proc.returncode not in (0, 1):  # 1 = no matches (fine)
        return None
    return proc.stdout.splitlines()


def _py_grep(pattern: str, limit: int) -> list[str]:
    """Pure-python fallback grep over *.lean (used only when no rg is available)."""
    try:
        rx = re.compile(pattern)
    except re.error as e:
        return [f"error: bad regex: {e}"]
    out: list[str] = []
    for path in MATHLIB_ROOT.rglob("*.lean"):
        try:
            with path.open(encoding="utf-8", errors="replace") as f:
                for i, ln in enumerate(f, 1):
                    if rx.search(ln):
                        out.append(f"{path}:{i}:{ln.rstrip()}")
                        if len(out) >= limit:
                            return out
        except OSError:
            continue
    return out


def tool_decl_grep(args: dict) -> str:
    pattern = (args.get("pattern") or "").strip()
    if not pattern:
        return "error: 'pattern' is required"
    try:
        limit = int(args.get("limit") or GREP_LIMIT)
    except (TypeError, ValueError):
        limit = GREP_LIMIT
    limit = max(1, min(limit, GREP_MAX))
    lines = _run_rg(pattern, limit)
    used = "ripgrep"
    if lines is None:
        lines = _py_grep(pattern, limit)
        used = "python-fallback"
    if lines and lines[0].startswith("error:"):
        return lines[0]
    if not lines:
        return f"No matches for pattern {pattern!r} under {MATHLIB_ROOT.name}/."
    out = [f"{len(lines)} match(es) for {pattern!r} (via {used}):"]
    for ln in lines[:limit]:
        # file:line:text — relativize the path to the checkout for brevity
        m = re.match(r"^(.*?):(\d+):(.*)$", ln)
        if not m:
            out.append(ln)
            continue
        fpath, lno, text = m.group(1), m.group(2), m.group(3).strip()
        try:
            rel = str(Path(fpath).resolve().relative_to(MATHLIB_ROOT.parent))
        except ValueError:
            rel = fpath
        out.append(f"- {rel}:{lno}\n    {text}")
    return cap("\n".join(out))


def _safe_path(file: str) -> Path | None:
    """Resolve `file` and confine it to MATHLIB_ROOT (read-only checkout)."""
    p = Path(file)
    if not p.is_absolute():
        # accept both "Mathlib/…" and "…/mathlib4/Mathlib/…" relative forms
        p = (MATHLIB_ROOT.parent / file) if str(file).startswith("Mathlib") \
            else (MATHLIB_ROOT / file)
    try:
        p = p.resolve()
        p.relative_to(MATHLIB_ROOT)
    except (ValueError, OSError):
        return None
    return p


def tool_decl_read(args: dict) -> str:
    file = (args.get("file") or "").strip()
    if not file:
        return "error: 'file' is required"
    try:
        line = int(args.get("line") or 1)
        n = int(args.get("n") or 20)
    except (TypeError, ValueError):
        return "error: 'line' and 'n' must be integers"
    n = max(1, min(n, READ_MAX))
    p = _safe_path(file)
    if p is None:
        return (f"error: {file!r} is outside the Mathlib checkout "
                f"({MATHLIB_ROOT}); read is refused.")
    if not p.exists() or not p.is_file():
        return f"error: no such file under the checkout: {file!r}"
    try:
        with p.open(encoding="utf-8", errors="replace") as f:
            all_lines = f.readlines()
    except OSError as e:
        return f"error: cannot read {file!r}: {e}"
    start = max(1, line)
    window = all_lines[start - 1: start - 1 + n]
    if not window:
        return f"error: line {line} is past EOF ({len(all_lines)} lines) in {file!r}."
    rel = str(p.relative_to(MATHLIB_ROOT.parent))
    body = "".join(f"{start + i:>6}  {ln.rstrip(chr(10))}\n"
                   for i, ln in enumerate(window))
    return cap(f"{rel}  (lines {start}–{start + len(window) - 1} of {len(all_lines)}):\n"
               + body)


def tool_loogle(args: dict) -> str:
    q = (args.get("q") or "").strip()
    if not q:
        return "error: 'q' is required"
    try:
        raw = http_get(LOOGLE_URL, {"q": q})
    except Exception as e:  # noqa: BLE003
        return f"loogle unavailable for {q!r}: {e}"
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return f"loogle returned non-JSON for {q!r}."
    if data.get("error"):
        return f"loogle error for {q!r}: {data['error']}"
    hits = data.get("hits", [])
    if not hits:
        return f"loogle: no declarations found for {q!r}."
    header = (data.get("header") or "").strip().splitlines()
    out = [header[0] if header else f"loogle hits for {q!r}:"]
    for h in hits[:LOOGLE_HITS]:
        name = h.get("name", "?")
        mod = h.get("module", "?")
        doc = (h.get("doc") or "").strip().replace("\n", " ")
        out.append(f"- {name}   [{mod}]" + (f"\n    {doc[:160]}" if doc else ""))
    return cap("\n".join(out))


TOOLS = {
    "loogle": {
        "description": ("Search Mathlib with Loogle: a name, a type pattern like "
                        "`(?a → ?b) → List ?a → List ?b`, or a conclusion pattern. "
                        "Returns matching Lean declaration names + their modules. "
                        "Zero-hallucination: every hit is a real Mathlib decl."),
        "inputSchema": {"type": "object",
                        "properties": {"q": {"type": "string",
                                             "description": "Loogle query/pattern"}},
                        "required": ["q"]},
        "handler": tool_loogle,
    },
    "decl_grep": {
        "description": ("Grep the pinned Mathlib source for declaration headers matching "
                        "a regex (e.g. `(theorem|lemma) .*IsCompact`). Returns file:line "
                        "+ the matched line. Read-only over the local checkout."),
        "inputSchema": {"type": "object",
                        "properties": {
                            "pattern": {"type": "string",
                                        "description": "regex to grep for"},
                            "limit": {"type": "integer",
                                      "description": f"max hits (default {GREP_LIMIT})"}},
                        "required": ["pattern"]},
        "handler": tool_decl_grep,
    },
    "decl_read": {
        "description": ("Read an n-line window of a Mathlib source file (as returned by "
                        "decl_grep) to see a declaration's full signature. Read-only; "
                        "paths confined to the Mathlib checkout."),
        "inputSchema": {"type": "object",
                        "properties": {
                            "file": {"type": "string",
                                     "description": "path under the Mathlib checkout"},
                            "line": {"type": "integer",
                                     "description": "1-based start line"},
                            "n": {"type": "integer",
                                  "description": f"lines to read (default 20, max {READ_MAX})"}},
                        "required": ["file", "line"]},
        "handler": tool_decl_read,
    },
}


# --------------------------------------------------------------------------- #
# Minimal JSON-RPC 2.0 stdio MCP loop (shared shape with wiki_mcp.py)           #
# --------------------------------------------------------------------------- #
def _result(mid, result):
    return {"jsonrpc": "2.0", "id": mid, "result": result}


def _error(mid, code, message):
    return {"jsonrpc": "2.0", "id": mid, "error": {"code": code, "message": message}}


def handle(msg: dict):
    method = msg.get("method")
    mid = msg.get("id")
    if method is None:
        return None
    if method == "initialize":
        proto = (msg.get("params") or {}).get("protocolVersion") or DEFAULT_PROTOCOL
        return _result(mid, {
            "protocolVersion": proto,
            "capabilities": {"tools": {}},
            "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
        })
    if method.startswith("notifications/"):
        return None
    if method == "ping":
        return _result(mid, {})
    if method == "tools/list":
        return _result(mid, {"tools": [
            {"name": n, "description": t["description"], "inputSchema": t["inputSchema"]}
            for n, t in TOOLS.items()]})
    if method == "tools/call":
        params = msg.get("params") or {}
        name = params.get("name")
        args = params.get("arguments") or {}
        tool = TOOLS.get(name)
        if tool is None:
            return _result(mid, {"content": [{"type": "text",
                                              "text": f"error: unknown tool {name!r}"}],
                                 "isError": True})
        try:
            text = tool["handler"](args)
            is_err = isinstance(text, str) and text.startswith("error:")
        except Exception as e:  # noqa: BLE003
            text, is_err = f"tool {name!r} failed: {e}", True
        return _result(mid, {"content": [{"type": "text", "text": str(text)}],
                             "isError": bool(is_err)})
    if mid is not None:
        return _error(mid, -32601, f"method not found: {method}")
    return None


def serve() -> int:
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            continue
        resp = handle(msg)
        if resp is not None:
            sys.stdout.write(json.dumps(resp, ensure_ascii=False) + "\n")
            sys.stdout.flush()
    return 0


def selftest() -> int:
    ok = True
    assert handle({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                   "params": {}})["result"]["serverInfo"]["name"] == SERVER_NAME
    assert len(handle({"jsonrpc": "2.0", "id": 2,
                       "method": "tools/list"})["result"]["tools"]) == 3
    calls = [("loogle", {"q": "Nat.Prime"}),
             ("decl_grep", {"pattern": r"^theorem exists_infinite_primes", "limit": 3}),
             ("decl_read", {"file": "Mathlib/Data/Nat/Prime/Infinite.lean",
                            "line": 33, "n": 3})]
    for name, args in calls:
        r = handle({"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                    "params": {"name": name, "arguments": args}})["result"]
        text = r["content"][0]["text"]
        ok = ok and bool(text)
        print(f"  {name}: isError={r.get('isError', False)} len={len(text)} "
              f":: {text[:70].replace(chr(10), ' ')}…")
    print("formal_mcp selftest OK" if ok else "formal_mcp selftest: empty tool output")
    return 0 if ok else 1


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        sys.exit(selftest())
    sys.exit(serve())
