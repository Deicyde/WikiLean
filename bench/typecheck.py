#!/usr/bin/env python3
"""bench/typecheck.py — the Bridge Experiment's Tier-1 typecheck rig.

Takes ONE Lean 4 declaration (a statement ending ``:= sorry`` plus whatever
``import`` lines it needs) and typechecks it against a PINNED Mathlib, returning a
JSON verdict:

    {"ok": bool, "errors": [...], "warnings": [...], "elapsed_s": float,
     "toolchain": "leanprover/lean4:v4.32.0-rc1", "mathlib_rev": "a33a5cc...",
     "timed_out": bool, "project": "...", "lean_commit": "..."}

``ok`` is true iff Lean reports **no** ``severity:"error"`` diagnostic and the check
neither timed out nor crashed. The ``sorry`` placeholder produces a *warning*
(``kind:"hasSorry"``), never an error — that is the expected, healthy result for a
statement-only task. Typecheck is NEVER "success" for the experiment (TheoremGraph:
22/24 typechecked, 5/24 correct); this rig only answers "does it compile against the
pinned library", which is the gate BEq+/judge grading sits behind.

WHY IT IS FAST (seconds, not minutes)
-------------------------------------
We do NOT run ``lake build`` and we do NOT run ``lake env lean`` per check. Both pay
Lake's manifest-resolution startup (~3 s here). Instead we resolve, ONCE, and cache:
  * the toolchain-pinned ``lean`` binary (``elan which lean`` in the project), and
  * the project's fully-resolved ``LEAN_PATH`` (``lake env printenv LEAN_PATH``),
then invoke ``LEAN_PATH=... <pinned-lean> --json <file>`` directly. Imports are
served from the project's prebuilt Mathlib olean cache (``lake exe cache get``), so a
single-file check loads only the transitive oleans it imports.

Measured warm latency on the reference machine (12-core M-series, Mathlib rev
a33a5cc, toolchain v4.32.0-rc1):
  * ``import Mathlib.Tactic.Basic``            ~1.0 s
  * ``import Mathlib.LinearAlgebra.Basis.Defs`` ~5.1 s   (peak RSS ~1.4 GB)
  * ``import Mathlib.Analysis.Fourier.AddCircle`` ~23.5 s (peak RSS ~2.0 GB)
  * ``import Mathlib`` (everything)             >120 s  -> DO NOT USE.

MINIMAL-IMPORT MODE (mandatory)
-------------------------------
``import Mathlib`` (the everything-import) does not finish inside any sane budget, so
the caller MUST pass only the modules the statement needs — either embedded as
``import`` lines in the code, or via ``--imports Mathlib.A,Mathlib.B`` which are
prepended. Tradeoff: the agent (or the task's recorded gold imports) has to name the
right modules; in exchange each check is seconds, not minutes. A statement whose
imports are wrong/insufficient simply fails typecheck with the real Lean error, which
is itself a fair signal for the experiment.

CONCURRENCY / SAFETY (bounded worker pool)
------------------------------------------
Each invocation typechecks exactly one declaration; the *caller* may fire many at
once (the harness runs ~50 arm x task cells over hours). A heavy check peaks near
2 GB RSS, so 50 truly-concurrent heavy checks would need ~100 GB — unsafe on a 19 GB
box. We therefore cap real parallelism with a **filesystem counting semaphore**: N
advisory-lock slot files in a shared lock dir. Every invocation blocks (up to
``--wait-timeout``) until it can ``flock`` one slot, runs Lean while holding it, and
releases on exit. So callers can launch as many as they like; at most N run at once,
the rest queue. Slots are keyed by ``max_workers`` so all invocations sharing that N
coordinate through the same pool. Default N = min(physical cores, RAM_GB // 3),
clamped to [1, 8] — chosen to keep peak memory well under RAM.

Because each check writes a unique temp ``.lean`` file (``tempfile``) and reads only
the shared, read-only Mathlib oleans, checks never interfere with one another and
never write into the Lean project (no ``.lake`` churn).

PERSISTENT-REPL MODE (``--server`` / ``BENCH_TC_SERVER``) — for ``import Mathlib``
---------------------------------------------------------------------------------
Single-shot is fatal for the ``import Mathlib`` everything-import (>120 s per check).
The ProofNet# gold set (``bench/data/bridge_tasks.jsonl``) *all* import Mathlib, and a
campaign is ~2 000 checks — impossible one-shot. So we add a long-lived server that
loads ``import Mathlib`` ONCE (~3 min) into a `leanprover-community/repl` process and
then elaborates each subsequent declaration against that in-memory environment in a
few seconds:

    # start the server (blocks; prints readiness after import Mathlib finishes)
    python3 bench/typecheck.py --server               # unix socket at /tmp/wikilean_tc.sock

    # any client (this same module, in-process or CLI) auto-routes to it:
    export BENCH_TC_SERVER=/tmp/wikilean_tc.sock
    echo 'theorem t : 1+1=2 := by norm_num' | python3 bench/typecheck.py

``repl`` (checkout ``/Users/jack/Desktop/LEAN/lean-repl`` at tag v4.32.0-rc1, matching
the pinned toolchain exactly) speaks JSON on stdin/stdout: ``{"cmd":"import Mathlib"}``
returns an env id; ``{"cmd":<decl>,"env":<id>}`` elaborates in that env and returns
``{"messages":[{severity,pos,data}],"sorries":[...]}``. The server wraps ONE repl
process behind a request lock; a ``ThreadingUnixStreamServer`` lets many client
processes submit concurrently — checks run ~sequentially (seconds each) rather than in
parallel (minutes each). Because every check builds on the pristine ``import Mathlib``
env (env 0, never a chain), checks are independent and can't leak decls into each other.

The client seam is transparent: ``typecheck()`` (hence ``score_bridge.typecheck_stub``,
unchanged) routes to the server when ``BENCH_TC_SERVER`` names a reachable socket, and
**falls back to single-shot** otherwise. The output contract is byte-identical (same
``ok/errors/warnings/elapsed_s/toolchain/mathlib_rev/lean_commit/project/timed_out``).
Since the env already has Mathlib, the server **drops every ``import`` line** from the
submitted code (you can't ``import`` against an existing env anyway) and elaborates the
remainder (the ``open …`` lines + the decl) against env 0. Per-check ``--timeout`` still
applies; a check that blows it is reported ``timed_out`` and the repl is recycled (the
shared stream would otherwise desync) — the next check re-imports Mathlib under the lock.
NB: a server restart re-pays the full ~3 min import (pickling the pure-imports env saves
nothing — unpickle still re-imports), so keep the server up for the whole campaign.

PINS (recorded in every response, per the preregistration)
----------------------------------------------------------
``toolchain``, ``mathlib_rev`` and ``lean_commit`` come straight from the project's
``lean-toolchain`` + ``lake-manifest.json`` + the pinned binary. Never compare rows
across different pins.

CLI CONTRACT
------------
Input (pick one; stdin is the default):
    echo 'CODE' | python3 bench/typecheck.py           # read Lean source from stdin
    python3 bench/typecheck.py --code 'CODE'           # inline
    python3 bench/typecheck.py --file stmt.lean         # from a file
    python3 bench/typecheck.py --imports Mathlib.X,Mathlib.Y --code 'theorem t : ... := sorry'

Options:
    --imports LIST     comma-separated modules -> prepended as `import` lines
    --timeout SEC      per-check wall-clock kill (default 60)
    --wait-timeout SEC max time to block for a worker slot (default 900)
    --max-workers N    concurrency cap / semaphore size (default auto)
    --project PATH     the pinned Lean+Mathlib project (default $BENCH_LEAN_PROJECT
                       or /Users/jack/Desktop/LEAN/wikifunctions)
    --refresh-env      force re-resolve of the cached LEAN_PATH / binary
    --pretty           pretty-print the JSON
    --print-env        print the resolved env cache and exit (diagnostic)

Output: a single JSON object on stdout (see top of docstring). Process exit code is
0 when the rig ran (regardless of ok/not-ok); non-zero only on a rig-level failure
(bad project, could not resolve toolchain, could not acquire a slot in time).
"""
from __future__ import annotations

import argparse
import contextlib
import fcntl
import json
import os
import re
import select
import signal
import socket
import socketserver
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

DEFAULT_PROJECT = os.environ.get(
    "BENCH_LEAN_PROJECT", "/Users/jack/Desktop/LEAN/wikifunctions"
)
HERE = Path(__file__).resolve().parent
ENV_CACHE = HERE / ".typecheck_env.json"

# Persistent-REPL server (see PERSISTENT-REPL MODE in the module docstring).
DEFAULT_REPL_BIN = os.environ.get(
    "BENCH_REPL_BIN", "/Users/jack/Desktop/LEAN/lean-repl/.lake/build/bin/repl"
)
DEFAULT_SOCKET = os.environ.get("BENCH_TC_SERVER", "/tmp/wikilean_tc.sock")
_IMPORT_LINE_RE = re.compile(r"(?m)^[ \t]*import[ \t]+\S.*$")


# --------------------------------------------------------------------------- env
def _project_signature(project: Path) -> str:
    """A cheap fingerprint that changes when the pin (toolchain/manifest) changes."""
    parts = []
    for name in ("lean-toolchain", "lake-manifest.json"):
        p = project / name
        try:
            parts.append(f"{name}:{p.stat().st_mtime_ns}:{p.stat().st_size}")
        except OSError:
            parts.append(f"{name}:missing")
    return "|".join(parts)


def _read_mathlib_rev(project: Path) -> str:
    try:
        m = json.loads((project / "lake-manifest.json").read_text())
        for pkg in m.get("packages", []):
            if pkg.get("name") == "mathlib":
                return pkg.get("rev", "unknown")
    except Exception:
        pass
    return "unknown"


def resolve_env(project: Path, *, refresh: bool = False) -> dict:
    """Resolve (and cache) the pinned lean binary + LEAN_PATH + pins for ``project``.

    Caches to bench/.typecheck_env.json keyed by the project path + its signature, so
    the expensive ``lake env`` / ``elan which`` calls run once, not per check.
    """
    sig = _project_signature(project)
    if not refresh and ENV_CACHE.exists():
        try:
            cached = json.loads(ENV_CACHE.read_text())
            if cached.get("project") == str(project) and cached.get("signature") == sig:
                # Sanity: the pinned binary still exists.
                if Path(cached["lean_bin"]).exists():
                    return cached
        except Exception:
            pass  # fall through to recompute

    if not (project / "lean-toolchain").exists():
        raise SystemExit(
            f"[typecheck] {project} is not a Lean project (no lean-toolchain). "
            f"Set --project or $BENCH_LEAN_PROJECT."
        )

    toolchain = (project / "lean-toolchain").read_text().strip()
    # Toolchain-pinned lean binary (bypasses cwd-based elan resolution at call time).
    lean_bin = subprocess.run(
        ["elan", "which", "lean"], cwd=project, capture_output=True, text=True
    ).stdout.strip()
    if not lean_bin or not Path(lean_bin).exists():
        raise SystemExit(f"[typecheck] could not resolve lean binary in {project}")

    # Fully-resolved import path for the project's built packages (incl. Mathlib cache).
    lean_path = subprocess.run(
        ["lake", "env", "printenv", "LEAN_PATH"],
        cwd=project,
        capture_output=True,
        text=True,
    ).stdout.strip()
    if not lean_path:
        raise SystemExit(
            f"[typecheck] empty LEAN_PATH from `lake env` in {project} — is the "
            f"project built / cache fetched (`lake exe cache get`)?"
        )

    lean_commit = ""
    with contextlib.suppress(Exception):
        ver = subprocess.run([lean_bin, "--version"], capture_output=True, text=True).stdout
        # e.g. "Lean (version 4.32.0-rc1, arm64-..., commit b4812ae53eea, Release)"
        if "commit " in ver:
            lean_commit = ver.split("commit ", 1)[1].split(",", 1)[0].strip()

    env = {
        "project": str(project),
        "signature": sig,
        "toolchain": toolchain,
        "lean_bin": lean_bin,
        "lean_path": lean_path,
        "lean_commit": lean_commit,
        "mathlib_rev": _read_mathlib_rev(project),
    }
    with contextlib.suppress(Exception):
        ENV_CACHE.write_text(json.dumps(env, indent=2))
    return env


# ------------------------------------------------------------------- concurrency
def _default_workers() -> int:
    try:
        cores = os.cpu_count() or 4
        ram_gb = os.sysconf("SC_PHYS_PAGES") * os.sysconf("SC_PAGE_SIZE") / 1e9
        return max(1, min(8, cores, int(ram_gb // 3)))
    except Exception:
        return 4


@contextlib.contextmanager
def worker_slot(max_workers: int, wait_timeout: float):
    """Block until one of ``max_workers`` semaphore slots is free, then hold it.

    Slots are advisory-locked files in a shared temp dir, keyed by ``max_workers`` so
    every co-running invocation with the same N contends over the same pool. Raises
    SystemExit if no slot frees within ``wait_timeout``.
    """
    lockdir = Path(tempfile.gettempdir()) / f"wikilean_typecheck_slots_{max_workers}"
    lockdir.mkdir(parents=True, exist_ok=True)
    deadline = time.time() + wait_timeout
    fd = None
    slot_i = None
    while True:
        for i in range(max_workers):
            f = os.open(str(lockdir / f"slot_{i}.lock"), os.O_CREAT | os.O_RDWR, 0o644)
            try:
                fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
                fd, slot_i = f, i
                break
            except OSError:
                os.close(f)
        if fd is not None:
            break
        if time.time() >= deadline:
            raise SystemExit(
                f"[typecheck] no worker slot free within {wait_timeout}s "
                f"(max_workers={max_workers})"
            )
        time.sleep(0.25)
    try:
        yield slot_i
    finally:
        with contextlib.suppress(Exception):
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)


# ------------------------------------------------------------------- the check
def _diag(obj: dict) -> dict:
    pos = obj.get("pos") or {}
    return {
        "line": pos.get("line"),
        "col": pos.get("column"),
        "message": obj.get("data", ""),
        "kind": obj.get("kind", ""),
    }


def _base_result(env: dict) -> dict:
    """The empty result skeleton — identical shape for single-shot and server."""
    return {
        "ok": False,
        "errors": [],
        "warnings": [],
        "elapsed_s": 0.0,
        "toolchain": env["toolchain"],
        "mathlib_rev": env["mathlib_rev"],
        "lean_commit": env.get("lean_commit", ""),
        "project": env["project"],
        "timed_out": False,
    }


def _strip_import_lines(code: str) -> str:
    """Drop every top-level ``import`` line — server mode already has Mathlib in env,
    and the repl forbids ``import`` against an existing env (README: "You can only use
    import commands when you do not specify the env field")."""
    return _IMPORT_LINE_RE.sub("", code)


# ------------------------------------------------------------- client (server seam)
def _server_socket_path() -> str | None:
    """The unix socket a client should route to, or None if server mode is off.

    Server mode is on when BENCH_TC_SERVER is set AND names an existing socket file.
    (An unset/empty var, or a missing socket, means single-shot.)
    """
    p = os.environ.get("BENCH_TC_SERVER", "").strip()
    if not p:
        return None
    return p if os.path.exists(p) else None


def _server_typecheck(code: str, env: dict, *, timeout: float) -> dict | None:
    """Submit one check to the persistent-REPL server. Returns the full result dict
    (same contract), or None if the server is unreachable (caller falls back to
    single-shot). The server itself strips imports and elaborates against env 0."""
    sock_path = _server_socket_path()
    if sock_path is None:
        return None
    req = json.dumps({"code": code, "timeout": timeout}).encode("utf-8")
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
            # Generous connect timeout; the read blocks for the check itself (which
            # may queue behind other clients on the server's single-repl lock).
            s.settimeout(max(30.0, timeout + 120.0))
            s.connect(sock_path)
            s.sendall(req)
            s.shutdown(socket.SHUT_WR)
            chunks = []
            while True:
                b = s.recv(65536)
                if not b:
                    break
                chunks.append(b)
    except (OSError, socket.timeout):
        return None  # unreachable / hung — fall back to single-shot
    if not chunks:
        return None
    try:
        return json.loads(b"".join(chunks).decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return None


def typecheck(
    code: str,
    env: dict,
    *,
    timeout: float,
    max_workers: int,
    wait_timeout: float,
) -> dict:
    """Typecheck one declaration. Transparently routes to the persistent-REPL server
    when ``BENCH_TC_SERVER`` names a reachable socket; otherwise single-shot. The
    signature and returned dict are identical either way (score_bridge relies on this).
    """
    if _server_socket_path() is not None:
        r = _server_typecheck(code, env, timeout=timeout)
        if r is not None:
            return r
        # else: server was named but unreachable — fall through to single-shot.
    return _single_shot_typecheck(
        code, env, timeout=timeout, max_workers=max_workers, wait_timeout=wait_timeout
    )


def _single_shot_typecheck(
    code: str,
    env: dict,
    *,
    timeout: float,
    max_workers: int,
    wait_timeout: float,
) -> dict:
    result = _base_result(env)

    run_env = dict(os.environ)
    run_env["LEAN_PATH"] = env["lean_path"]

    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".lean", prefix="bench_tc_", delete=False
    ) as tf:
        tf.write(code if code.endswith("\n") else code + "\n")
        tmp_path = tf.name

    t0 = time.time()
    try:
        with worker_slot(max_workers, wait_timeout):
            try:
                proc = subprocess.run(
                    [env["lean_bin"], "--json", tmp_path],
                    env=run_env,
                    capture_output=True,
                    text=True,
                    timeout=timeout,
                )
                stdout, stderr, rc = proc.stdout, proc.stderr, proc.returncode
            except subprocess.TimeoutExpired:
                result["elapsed_s"] = round(time.time() - t0, 3)
                result["timed_out"] = True
                result["errors"] = [
                    {
                        "line": None,
                        "col": None,
                        "message": f"typecheck exceeded {timeout}s budget",
                        "kind": "timeout",
                    }
                ]
                return result
    finally:
        with contextlib.suppress(OSError):
            os.unlink(tmp_path)

    result["elapsed_s"] = round(time.time() - t0, 3)

    for line in stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        sev = obj.get("severity")
        if sev == "error":
            result["errors"].append(_diag(obj))
        elif sev == "warning":
            result["warnings"].append(_diag(obj))
        # info/trace diagnostics are ignored

    # A nonzero exit with no parsed error diagnostic means Lean crashed/panicked
    # before emitting JSON (e.g. a stack overflow or an interpreter panic). Surface
    # stderr so the failure is never silently swallowed.
    if rc != 0 and not result["errors"]:
        msg = stderr.strip() or f"lean exited with code {rc} and no diagnostics"
        result["errors"].append(
            {"line": None, "col": None, "message": msg, "kind": "lean-nonzero-exit"}
        )

    result["ok"] = not result["errors"] and not result["timed_out"]
    return result


# -------------------------------------------------------------- persistent server
class _ReplTimeout(Exception):
    """The repl did not answer a command within the deadline."""


class _ReplDead(Exception):
    """The repl process exited / the stream desynced."""


class ReplServer:
    """Wraps ONE `leanprover-community/repl` process that has ``import Mathlib`` loaded,
    and serves typecheck requests behind a single lock. Thread-safe: every check
    acquires ``self.lock``, so concurrent client threads queue and run ~sequentially.

    A pathological check that exceeds its per-call timeout recycles the repl (kill +
    lazy re-import) so one bad statement can't wedge the campaign.
    """

    def __init__(self, env: dict, repl_bin: str):
        self.env = env
        self.repl_bin = repl_bin
        self.lock = threading.Lock()
        self.proc: subprocess.Popen | None = None
        self.env_id: int | None = None
        self._buf = b""
        self.n_checks = 0
        self.n_imports = 0

    # -- low-level repl I/O (must hold self.lock) --------------------------------
    def _spawn(self) -> None:
        run_env = dict(os.environ)
        run_env["LEAN_PATH"] = self.env["lean_path"]
        run_env.pop("ANTHROPIC_API_KEY", None)  # project max-auth gotcha; harmless here
        self.proc = subprocess.Popen(
            [self.repl_bin],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            bufsize=0,  # unbuffered binary; we frame lines ourselves over the raw fd
            env=run_env,
            cwd=self.env["project"],
        )
        self._buf = b""

    def _kill(self) -> None:
        if self.proc is not None:
            with contextlib.suppress(Exception):
                self.proc.kill()
            with contextlib.suppress(Exception):
                self.proc.wait(timeout=10)
        self.proc = None
        self.env_id = None
        self._buf = b""

    def _readline(self, deadline: float) -> bytes:
        """Read one line (sans trailing newline) from the repl, honoring a deadline."""
        assert self.proc is not None and self.proc.stdout is not None
        fd = self.proc.stdout.fileno()
        while b"\n" not in self._buf:
            remaining = deadline - time.time()
            if remaining <= 0:
                raise _ReplTimeout()
            r, _, _ = select.select([fd], [], [], remaining)
            if not r:
                raise _ReplTimeout()
            chunk = os.read(fd, 65536)
            if not chunk:
                raise _ReplDead("repl stdout closed (process died)")
            self._buf += chunk
        line, self._buf = self._buf.split(b"\n", 1)
        return line

    def _exchange(self, obj: dict, timeout: float) -> dict:
        """Send one JSON command, read its response (JSON up to the blank-line
        separator). Must hold self.lock."""
        assert self.proc is not None and self.proc.stdin is not None
        payload = (json.dumps(obj) + "\n\n").encode("utf-8")
        try:
            self.proc.stdin.write(payload)
            self.proc.stdin.flush()
        except (BrokenPipeError, OSError) as e:
            raise _ReplDead(f"repl stdin write failed: {e}")
        deadline = time.time() + timeout
        lines: list[bytes] = []
        while True:
            line = self._readline(deadline)
            if line.strip() == b"":
                if lines:
                    break
                continue  # leading blank line — keep waiting for content
            lines.append(line)
        text = b"\n".join(lines).decode("utf-8", "replace")
        try:
            return json.loads(text)
        except ValueError as e:
            raise _ReplDead(f"unparseable repl response: {e}: {text[:400]!r}")

    def _ensure_imported(self) -> None:
        """Guarantee a live repl with ``import Mathlib`` loaded. Must hold self.lock."""
        if self.proc is not None and self.proc.poll() is None and self.env_id is not None:
            return
        self._kill()
        self._spawn()
        t0 = time.time()
        # The import is the slow one; give it a very generous ceiling (~10 min).
        resp = self._exchange({"cmd": "import Mathlib"}, timeout=900.0)
        self.env_id = resp.get("env")
        if self.env_id is None:
            raise _ReplDead(f"import Mathlib returned no env: {resp}")
        self.n_imports += 1
        dt = time.time() - t0
        print(
            f"[typecheck-server] import Mathlib loaded in {dt:.1f}s "
            f"(env={self.env_id}, import #{self.n_imports})",
            flush=True,
        )

    # -- public: one check -------------------------------------------------------
    def check(self, code: str, timeout: float) -> dict:
        result = _base_result(self.env)
        stripped = _strip_import_lines(code)
        with self.lock:
            try:
                self._ensure_imported()
            except Exception as e:  # import failed — surface, don't crash the server
                result["errors"].append(
                    {"line": None, "col": None,
                     "message": f"server could not import Mathlib: {e}",
                     "kind": "server-init"}
                )
                self._kill()
                return result
            t0 = time.time()
            try:
                resp = self._exchange({"cmd": stripped, "env": self.env_id}, timeout)
            except _ReplTimeout:
                result["elapsed_s"] = round(time.time() - t0, 3)
                result["timed_out"] = True
                result["errors"].append(
                    {"line": None, "col": None,
                     "message": f"typecheck exceeded {timeout}s budget",
                     "kind": "timeout"}
                )
                self._kill()  # stream is desynced; force re-import next check
                return result
            except _ReplDead as e:
                result["elapsed_s"] = round(time.time() - t0, 3)
                result["errors"].append(
                    {"line": None, "col": None,
                     "message": f"repl error: {e}", "kind": "repl-error"}
                )
                self._kill()
                return result
            result["elapsed_s"] = round(time.time() - t0, 3)
            self.n_checks += 1

        # Parse outside the lock. A repl Error object is {"message": ...} with no env.
        if isinstance(resp, dict) and "env" not in resp and "message" in resp:
            result["errors"].append(
                {"line": None, "col": None,
                 "message": str(resp["message"]), "kind": "repl-error"}
            )
        else:
            for m in resp.get("messages", []) or []:
                pos = m.get("pos") or {}
                diag = {
                    "line": pos.get("line"),
                    "col": pos.get("column"),
                    "message": m.get("data", ""),
                    "kind": "",
                }
                sev = m.get("severity")
                if sev == "error":
                    result["errors"].append(diag)
                elif sev == "warning":
                    result["warnings"].append(diag)
                # info/trace ignored (matches single-shot)
        result["ok"] = not result["errors"] and not result["timed_out"]
        return result


def run_server(env: dict, socket_path: str, repl_bin: str) -> int:
    """Start the persistent-REPL server: import Mathlib once, then serve checks over a
    unix socket until SIGINT/SIGTERM. Blocks."""
    if not Path(repl_bin).exists():
        raise SystemExit(
            f"[typecheck-server] repl binary not found: {repl_bin}\n"
            f"  Build it: cd /Users/jack/Desktop/LEAN/lean-repl && lake build repl\n"
            f"  or set $BENCH_REPL_BIN."
        )
    with contextlib.suppress(FileNotFoundError):
        os.unlink(socket_path)  # clear a stale socket from a prior run

    backend = ReplServer(env, repl_bin)
    # Warm it now, so the socket only appears once checks can actually be served.
    print(f"[typecheck-server] importing Mathlib (one-time, ~3 min) …", flush=True)
    with backend.lock:
        backend._ensure_imported()

    class Handler(socketserver.BaseRequestHandler):
        def handle(self):
            chunks = []
            while True:
                b = self.request.recv(65536)
                if not b:
                    break
                chunks.append(b)
            try:
                req = json.loads(b"".join(chunks).decode("utf-8"))
            except Exception:
                self.request.sendall(b'{"error":"bad request"}')
                return
            if req.get("ping"):
                self.request.sendall(json.dumps({
                    "ok": True, "n_checks": backend.n_checks,
                    "n_imports": backend.n_imports, "toolchain": env["toolchain"],
                    "mathlib_rev": env["mathlib_rev"],
                }).encode("utf-8"))
                return
            code = req.get("code", "")
            timeout = float(req.get("timeout", 90.0))
            result = backend.check(code, timeout)
            self.request.sendall(json.dumps(result).encode("utf-8"))

    class Server(socketserver.ThreadingUnixStreamServer):
        daemon_threads = True
        allow_reuse_address = True

    srv = Server(socket_path, Handler)

    def _shutdown(signum, frame):
        print(f"\n[typecheck-server] signal {signum} — shutting down "
              f"({backend.n_checks} checks served)", flush=True)
        threading.Thread(target=srv.shutdown, daemon=True).start()

    signal.signal(signal.SIGINT, _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)

    print(
        f"[typecheck-server] READY on {socket_path}\n"
        f"  toolchain={env['toolchain']}  mathlib_rev={env['mathlib_rev'][:12]}\n"
        f"  clients: export BENCH_TC_SERVER={socket_path}",
        flush=True,
    )
    try:
        srv.serve_forever()
    finally:
        srv.server_close()
        backend._kill()
        with contextlib.suppress(FileNotFoundError):
            os.unlink(socket_path)
    return 0


# ------------------------------------------------------------------------- cli
def _gather_code(args) -> str:
    if args.code is not None:
        body = args.code
    elif args.file:
        body = Path(args.file).read_text()
    else:
        body = sys.stdin.read()
    imports = ""
    if args.imports:
        mods = [m.strip() for m in args.imports.split(",") if m.strip()]
        imports = "".join(f"import {m}\n" for m in mods)
    return imports + body


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Typecheck one Lean 4 declaration against a pinned Mathlib.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    src = ap.add_mutually_exclusive_group()
    src.add_argument("--code", help="inline Lean source")
    src.add_argument("--file", help="read Lean source from this file")
    ap.add_argument(
        "--imports",
        help="comma-separated modules prepended as `import` lines (minimal-import mode)",
    )
    ap.add_argument("--timeout", type=float, default=60.0, help="per-check kill (s)")
    ap.add_argument(
        "--wait-timeout",
        type=float,
        default=900.0,
        help="max time to block for a worker slot (s)",
    )
    ap.add_argument(
        "--max-workers",
        type=int,
        default=_default_workers(),
        help="concurrency cap / semaphore size (default auto by cores+RAM)",
    )
    ap.add_argument("--project", default=DEFAULT_PROJECT, help="pinned Lean project")
    ap.add_argument("--refresh-env", action="store_true", help="re-resolve env cache")
    ap.add_argument("--pretty", action="store_true", help="pretty-print JSON")
    ap.add_argument(
        "--print-env", action="store_true", help="print resolved env cache and exit"
    )
    ap.add_argument(
        "--server", action="store_true",
        help="run the persistent-REPL server (import Mathlib once, serve over a socket)",
    )
    ap.add_argument(
        "--socket", default=DEFAULT_SOCKET,
        help=f"unix socket path for --server / clients (default {DEFAULT_SOCKET})",
    )
    ap.add_argument(
        "--repl-bin", default=DEFAULT_REPL_BIN,
        help=f"leanprover-community/repl binary (default {DEFAULT_REPL_BIN})",
    )
    args = ap.parse_args()

    env = resolve_env(Path(args.project).resolve(), refresh=args.refresh_env)

    if args.print_env:
        print(json.dumps(env, indent=2))
        return 0

    if args.server:
        # Note: as the server, we do NOT read BENCH_TC_SERVER as a client would — the
        # --socket path is where we LISTEN. (BENCH_TC_SERVER is for clients.)
        return run_server(env, args.socket, args.repl_bin)

    code = _gather_code(args)
    if not code.strip():
        print(json.dumps({"ok": False, "errors": [
            {"line": None, "col": None, "message": "empty input", "kind": "no-input"}
        ], "warnings": [], "elapsed_s": 0.0, "toolchain": env["toolchain"],
            "mathlib_rev": env["mathlib_rev"], "timed_out": False}))
        return 0

    res = typecheck(
        code,
        env,
        timeout=args.timeout,
        max_workers=args.max_workers,
        wait_timeout=args.wait_timeout,
    )
    print(json.dumps(res, indent=2 if args.pretty else None))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
