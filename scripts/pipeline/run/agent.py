#!/usr/bin/env python3
"""Stage 10: one agent call, sandboxed, with the envelope the watchdog prices.

docs/PIPELINE.md section 3 (stage 10). Every model call of this pipeline goes through
``site/agent_sandbox.sandboxed_options`` — WikiLean's hard invariant, enforced by
``site/test_agent_sandbox.py``: the read tools and never Bash, never
``bypassPermissions``, a working directory outside every checkout, and no user or
project settings. A locate worker reads untrusted article text, which is the
prompt-injection surface that invariant exists for, so the pipeline's agents are the
last place to make an exception.

The one thing a worker may do besides read is **write its own document**, into
``work/<task-id>/findings/<task-id>/`` and nowhere else: a stage whose whole output is
one file needs a tool that can write one. That is ``sandboxed_options(write_dir=…)``,
decided per call by an in-process PreToolUse hook, and nothing else moved — no shell,
no settings, no read outside the working directory. Everything else the old prompts
told a worker to *run* — the passage ids, the gate — the **runner** runs, between the
worker and the reviewer (:mod:`task`).

Every call leaves an **envelope** under ``<root>/usage/``, named
``<task-id>.<role>.<UTC stamp>.<pid>.json``. A call that returned a priced result writes
the SDK's result object; a call that timed out, was signalled or returned nothing writes
``{"total_cost_usd": null, "agent_call": {"elapsed_s": …, "why": …}}`` — because a call
that left no priced envelope is not a free call, and the watchdog estimates it rather
than counting zero.

**Nothing here runs a model by default.** :func:`call` needs ``--execute``; without it
the call is planned, the envelope is written as a dry run and no SDK import happens, so
a plan, a gate and a harvest can be exercised end to end on a host with no credentials
— which is what the synthetic run of :mod:`test_run` does. ``--worker-command`` runs a
local command in place of the model, which is how a contributor's stub worker and the
tests produce documents.

Ported in spirit from ``run/agent_call.py``: the envelope's name and its unpriced
record are that module's, because the watchdog reads them. The sandbox is WikiLean's own
and replaces agentbox, which is a Linux run host's tool and does not move.

    python3 scripts/pipeline/run/agent.py --root <root> --task <task-id> --role worker \\
        --prompt <file> [--system <file>] [--model <name>] \\
        [--worker-command '<cmd>' | --execute]
"""
from __future__ import annotations

import argparse
import json
import os
import shlex
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Mapping

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
for _candidate in (HERE, HERE.parent, ROOT / "site"):
    if str(_candidate) not in sys.path:
        sys.path.insert(0, str(_candidate))

#: The roles a call may have; the envelope's name carries one.
ROLES = ("worker", "reviewer")
#: What a worker may see at all. A locate worker reads its task manifest, the
#: article's rendered views and the declaration search out of its own working
#: directory, and writes the one document its task exists for into the one directory
#: under it the sandbox opens (``write_dir``). Nothing else, and never a shell.
WORKER_TOOLS = ("Read", "Grep", "Glob", "Write")
#: A reviewer reads and writes nothing: its verdict is its own text.
REVIEWER_TOOLS = ("Read", "Grep", "Glob")
#: Where a worker writes, under its working directory. ``run/workdir.py`` makes it.
FINDINGS = "findings"


def tools_for(role: str) -> tuple[str, ...]:
    """The tools of a role: a worker writes its document, a reviewer writes nothing."""
    return WORKER_TOOLS if role == "worker" else REVIEWER_TOOLS
#: The default turn cap of one call.
MAX_TURNS = 60


class AgentProblem(ValueError):
    """A call that cannot be made as asked, named rather than made another way."""


def stamp() -> str:
    return time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())


def envelope_path(root: Path, task_id: str, role: str, *, step: str | None = None) -> Path:
    """``<root>/usage/<task-id>.<role>[.<step>].<stamp>.<pid>.json``."""
    if role not in ROLES:
        raise AgentProblem(f"a call's role is one of {', '.join(ROLES)}, not {role!r}")
    parts = [task_id, role] + ([step] if step else []) + [stamp(), str(os.getpid())]
    return Path(root) / "usage" / (".".join(parts) + ".json")


def write_envelope(path: Path, value: Mapping[str, Any]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=1, sort_keys=True) + "\n", encoding="utf-8")
    return path


def unpriced(elapsed: float, why: str) -> dict:
    """The record of a call that left no priced envelope: its seconds, never nothing."""
    return {"total_cost_usd": None, "is_error": True,
            "agent_call": {"elapsed_s": round(max(0.0, elapsed), 3), "why": why}}


def options(*, model: str | None, system_prompt: str, cwd: Path,
            tools=WORKER_TOOLS, max_turns: int = MAX_TURNS,
            write_dir: Path | None = None):
    """The one place this pipeline builds agent options: ``site/agent_sandbox``'s.

    ``write_dir`` is the only directory ``Write`` reaches, and it is under ``cwd``:
    a worker's whole output is one document, and an agent that cannot write one
    produces nothing. Everything else stays exactly as strict — no Bash, no settings,
    reads scoped to ``cwd``.

    Imported here and not at module scope, so a host with no SDK can still plan, gate
    and harvest a run.
    """
    import agent_sandbox  # noqa: PLC0415 — site/agent_sandbox.py

    return agent_sandbox.sandboxed_options(model=model, system_prompt=system_prompt,
                                           tools=list(tools), max_turns=max_turns,
                                           cwd=cwd, write_dir=write_dir)


def run_command(command: str, *, cwd: Path, timeout: float) -> tuple[int, str, float]:
    """Run a local command in place of the model; return ``(code, output, seconds)``."""
    started = time.monotonic()
    try:
        proc = subprocess.run(shlex.split(command), cwd=str(cwd), capture_output=True,
                              text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return 124, "", time.monotonic() - started
    except OSError as exc:
        return 127, str(exc), time.monotonic() - started
    return proc.returncode, (proc.stdout + proc.stderr)[-4000:], time.monotonic() - started


def call(*, root: Path, task_id: str, role: str, prompt: str, system: str = "",
         model: str | None = None, worker_command: str = "", execute: bool = False,
         timeout: float = 5400.0, step: str | None = None) -> dict:
    """Make one call and write its envelope; return the envelope.

    Three ways, in the order a host will meet them:

    * ``worker_command`` — a local command stands in for the model. The envelope is
      unpriced with the command's elapsed time, because nothing was spent on a model and
      the watchdog must not read a zero as a priced zero.
    * ``execute`` — the real call, through the sandbox. The SDK's result object is the
      envelope.
    * neither — a dry run: the call is described and nothing is spent. This is the
      default, so no command line can start a paid run by omission.
    """
    root = Path(root)
    workdir = root / "work" / task_id
    workdir.mkdir(parents=True, exist_ok=True)
    # a worker writes into findings/<task-id>/ and nowhere else; a reviewer writes
    # nothing at all, and its verdict is the text of its own answer
    write_dir = (workdir / FINDINGS / task_id) if role == "worker" else None
    if write_dir is not None:
        write_dir.mkdir(parents=True, exist_ok=True)
    path = envelope_path(root, task_id, role, step=step)
    if worker_command:
        code, output, elapsed = run_command(worker_command, cwd=workdir, timeout=timeout)
        envelope = {**unpriced(elapsed, f"a local command stood in for the model (exit "
                                        f"{code})"),
                    "is_error": code != 0,
                    "agent_call": {"elapsed_s": round(elapsed, 3), "why": "worker_command",
                                   "exit": code, "command": worker_command,
                                   "output": output[-1000:]},
                    "task_id": task_id, "role": role}
        write_envelope(path, envelope)
        return envelope
    if not execute:
        envelope = {"total_cost_usd": 0.0, "is_error": False, "dry_run": True,
                    "task_id": task_id, "role": role,
                    "agent_call": {"elapsed_s": 0.0, "why": "dry run: no model was called",
                                   "model": model, "prompt_chars": len(prompt)}}
        write_envelope(path, envelope)
        return envelope
    started = time.monotonic()
    try:
        envelope = _execute(prompt=prompt, system=system, model=model, workdir=workdir,
                            tools=tools_for(role), write_dir=write_dir, timeout=timeout)
    except Exception as exc:  # noqa: BLE001 — any failure is still a call that spent time
        envelope = unpriced(time.monotonic() - started,
                            f"{exc.__class__.__name__}: {exc}")
    envelope.setdefault("task_id", task_id)
    envelope.setdefault("role", role)
    write_envelope(path, envelope)
    return envelope


def _execute(*, prompt: str, system: str, model: str | None, workdir: Path,
             tools=WORKER_TOOLS, write_dir: Path | None = None,
              timeout: float = 5400.0) -> dict:
    """The real call. Imported lazily; the result object is the envelope."""
    import asyncio  # noqa: PLC0415

    from claude_agent_sdk import query  # noqa: PLC0415

    opts = options(model=model, system_prompt=system, cwd=workdir, tools=tools,
                   write_dir=write_dir)

    async def _go() -> dict:
        result: dict = {}
        async with asyncio.timeout(timeout):
            async for message in query(prompt=prompt, options=opts):
                payload = getattr(message, "__dict__", None)
                if isinstance(payload, dict) and payload.get("subtype") in (None, "success") \
                        and "total_cost_usd" in payload:
                    result = dict(payload)
        return result

    got = asyncio.run(_go())
    if not got:
        raise AgentProblem("the call returned no result object")
    return got


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--root", required=True, type=Path)
    parser.add_argument("--task", required=True)
    parser.add_argument("--role", default="worker", choices=ROLES)
    parser.add_argument("--step", default=None)
    parser.add_argument("--prompt", required=True, type=Path)
    parser.add_argument("--system", type=Path)
    parser.add_argument("--model", default=None)
    parser.add_argument("--worker-command", default="",
                        help="a local command that stands in for the model")
    parser.add_argument("--execute", action="store_true",
                        help="make the real call; without it nothing is spent")
    parser.add_argument("--timeout", type=float, default=5400.0)
    args = parser.parse_args(argv)
    if args.execute and args.worker_command:
        print("agent: --execute and --worker-command name two different calls",
              file=sys.stderr)
        return 2
    try:
        envelope = call(root=args.root, task_id=args.task, role=args.role,
                        prompt=args.prompt.read_text(encoding="utf-8"),
                        system=args.system.read_text(encoding="utf-8") if args.system else "",
                        model=args.model, worker_command=args.worker_command,
                        execute=args.execute, timeout=args.timeout, step=args.step)
    except (AgentProblem, OSError) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    print(json.dumps({key: envelope.get(key)
                      for key in ("task_id", "role", "total_cost_usd", "is_error")},
                     sort_keys=True))
    return 1 if envelope.get("is_error") else 0


if __name__ == "__main__":
    raise SystemExit(main())
