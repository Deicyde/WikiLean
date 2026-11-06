#!/usr/bin/env python3
"""Stage 10: the per-shard spend watchdog — drain a root before it overspends.

docs/PIPELINE.md section 3 (stage 10). Model-free and standard library only: it decides
from numbers, runs on the host, and its one action is to **drain** the root — finish what
is in flight, start nothing. What it reads, all of it outside every worktree:

* **spend** — the sum of ``total_cost_usd`` over the agents' JSON envelopes under
  ``<root>/usage/``, **plus an estimate for every call that ended with no priced
  envelope** (a timeout, a signal, an empty stdout: the call is recorded with
  ``total_cost_usd: null`` and its elapsed time). The estimate is the call's seconds
  times the highest dollars-per-second any priced envelope of the root shows, or
  ``--unpriced-usd-per-hour`` before one exists. A 45-minute call that timed out used to
  add nothing at all, which is the defect this rule exists for;
* **the gate-failure rate** — the fraction of the gate's *validation* calls in
  ``<root>/gate-log/<run>.jsonl`` that did not pass. An agent's own ``--findings`` check
  is logged too and counts for nothing here, and the batched pre-merge re-check is
  counted apart, so a worker that re-runs the gate neither drains the root nor dilutes
  its rate;
* **merged tasks** — how many task directories are on the merge target.

**The per-task rule's two lessons**, which this port is built around:

1. **It must not false-trip before enough tasks merge.** Cost per merged task over two
   tasks is noise: one slow article doubles it. The rule is held back until
   ``--min-tasks`` have merged, and it does not fire at all when the merged count could
   not be read — an unreadable repository is not evidence of overspending. The
   gate-failure rule has the same floor in ``--min-gate-calls``.
2. **It must not exit leaving a cap unenforced after a drain.** A drain stops new tasks;
   the calls already in flight keep spending, and the original watchdog exited the moment
   the drain returned. So after a drain this one **settles**: it keeps checking until the
   spend stops moving (``--settle-checks`` consecutive checks with no change) or
   ``--settle-timeout`` seconds pass, and its last record carries ``settled``,
   ``spend_after_drain_usd`` and ``over_cap_after_drain``. It exits 2 for a clean drain
   and 3 when the cap was still over after settling, which is a stop for the campaign
   rather than a line in a log nobody read.

Every check is appended to ``<root>/watchdog.jsonl`` and the latest is written to
``<root>/watchdog.json``. It stops on ``<root>/watchdog.stop``.

Ported from ``run/watchdog.py``. What changes: the two lessons above, the gate log is
read by the gate's own line shape, and the drain command is named on the command line
rather than assumed to be tend's — the substrate is the host's, and WikiLean's pipeline
drives it rather than vendoring it.

    python3 scripts/pipeline/run/watchdog.py --root <tend root> --max-cost 120 \\
        [--p90-cost-per-task 4.1] [--p90-gate-failure-rate 0.12] [--multiple 2] \\
        [--drain-command 'tend-control drain --root {root}'] \\
        [--interval 300 | --once] [--dry-run]
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import subprocess
import sys
import time
from pathlib import Path

#: Below this many merged tasks the cost-per-task rule does not fire.
MIN_TASKS = 5
#: Below this many validation calls the failure-rate rule does not fire.
MIN_GATE_CALLS = 20
#: Seconds between looks at the stop file while it waits for the next check.
POLL = 2.0
#: How long the settling phase after a drain runs, and how many quiet checks end it.
SETTLE_TIMEOUT = 1800.0
SETTLE_CHECKS = 3
SETTLE_INTERVAL = 30.0
#: The roles an envelope's name may carry, and the audited steps between them.
ROLES = ("worker", "reviewer")
AUDIT_STEPS = ("audit", "skeptic", "check", "revision")
_ENVELOPE = re.compile(r"(?P<head>.+)\.(?P<stamp>\d{8}T\d{6}Z)\.(?P<pid>\d+)\.(?:json|out)\Z")

#: Exit codes, which are the watchdog's contract with whatever sequences the shards.
EXIT_OK, EXIT_USAGE, EXIT_DRAINED, EXIT_OVER_CAP = 0, 64, 2, 3


def _number(value) -> float | None:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    return None


#: Where a runner records what a call that left no priced envelope did. This pipeline
#: writes ``agent_call``; a run host that wrote ``cf_agent_call`` is read too, so a
#: recorded run can be replayed against this watchdog without rewriting its envelopes.
CALL_KEYS = ("agent_call", "cf_agent_call")


def call_seconds(envelope: dict) -> float | None:
    """How long an unpriced call ran, from its own record; None when it kept none.

    The record's ``elapsed_s`` is the authority even when it is ``0.0`` — a call that
    returned empty in eight milliseconds cost nothing, and reading the harness's
    ``duration_ms`` as the agent's time would price thousands of them as if they had run.
    """
    for key in CALL_KEYS:
        record = envelope.get(key)
        if isinstance(record, dict):
            value = _number(record.get("elapsed_s"))
            if value is not None:
                return value
    return None


def envelope_name(name: str) -> dict | None:
    """``{task_id, role, step, stamp, pid}`` of an envelope's file name, or None.

    Both forms: ``<task>.<role>.<stamp>.<pid>.json`` and an audited call's
    ``<task>.<role>.<step>.<stamp>.<pid>.json``. A task id may itself hold dots, so the
    name is read from its right end.
    """
    match = _ENVELOPE.match(name)
    if not match:
        return None
    head = match.group("head")
    step = None
    task, _, last = head.rpartition(".")
    if last in AUDIT_STEPS:
        step = last
        task, _, last = task.rpartition(".")
    if last not in ROLES or not task:
        return None
    return {"task_id": task, "role": last, "step": step, "stamp": match.group("stamp"),
            "pid": int(match.group("pid"))}


def ledger(usage_dir: Path, *, fallback_usd_per_hour: float | None = None) -> dict:
    """What ``usage/`` says was spent: the priced floor and the estimate of the rest.

    ``{dollars, envelopes, unpriced_calls, unpriced_seconds, usd_per_second, usd_per_hour,
    estimated_unpriced_usd[, by_step]}``. An envelope with a ``total_cost_usd`` is priced;
    one whose ``total_cost_usd`` is null and that records an elapsed time is an unpriced
    call, estimated at the highest dollars-per-second of the priced envelopes that carry
    a duration — or ``fallback_usd_per_hour`` before any does.
    ``estimated_unpriced_usd`` is None when neither exists, which is the honest answer:
    the call cost something and nothing here can say how much.
    """
    out = {"dollars": 0.0, "envelopes": 0, "unpriced_calls": 0, "unpriced_seconds": 0.0,
           "usd_per_second": None, "usd_per_hour": None, "estimated_unpriced_usd": 0.0}
    usage_dir = Path(usage_dir)
    if not usage_dir.is_dir():
        return out
    rate = None
    by_step: dict[str, dict] = {}
    for path in sorted(usage_dir.rglob("*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(data, dict):
            continue
        out["envelopes"] += 1
        cost = _number(data.get("total_cost_usd"))
        millis = _number(data.get("duration_ms"))
        named = envelope_name(path.name)
        if named and named["step"]:
            row = by_step.setdefault(named["step"], {"envelopes": 0, "dollars": 0.0})
            row["envelopes"] += 1
            row["dollars"] += cost if cost is not None and cost > 0 else 0.0
        if cost is not None:
            if cost > 0:
                out["dollars"] += cost
                if millis and millis > 0:
                    here = cost / (millis / 1000.0)
                    rate = here if rate is None else max(rate, here)
            continue
        seconds = call_seconds(data)
        if seconds is None and millis is not None:
            seconds = millis / 1000.0
        if seconds is not None:
            out["unpriced_calls"] += 1
            out["unpriced_seconds"] += max(0.0, seconds)
    if rate is None and fallback_usd_per_hour is not None:
        rate = float(fallback_usd_per_hour) / 3600.0
    out["usd_per_second"] = round(rate, 9) if rate is not None else None
    out["usd_per_hour"] = round(rate * 3600.0, 6) if rate is not None else None
    if out["unpriced_calls"]:
        out["estimated_unpriced_usd"] = (round(out["unpriced_seconds"] * rate, 6)
                                         if rate is not None else None)
    out["dollars"] = round(out["dollars"], 6)
    out["unpriced_seconds"] = round(out["unpriced_seconds"], 1)
    if by_step:
        out["by_step"] = {step: {"envelopes": row["envelopes"],
                                 "dollars": round(row["dollars"], 6)}
                          for step, row in sorted(by_step.items())}
    return out


def gate_counts(log_dir: Path) -> dict[str, int]:
    """``{calls, failed, pre_merge_calls, pre_merge_failed}`` over the gate's own log.

    Only a validation call — one that judged a change set and is not the batched
    pre-merge re-check — is one of ``calls``.
    """
    out = {"calls": 0, "failed": 0, "pre_merge_calls": 0, "pre_merge_failed": 0}
    log_dir = Path(log_dir)
    paths = [log_dir] if log_dir.is_file() else (
        sorted(log_dir.glob("*.jsonl")) if log_dir.is_dir() else [])
    for path in paths:
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except OSError:
            continue
        for line in lines:
            try:
                record = json.loads(line)
            except ValueError:
                continue
            if not isinstance(record, dict) or record.get("changed_files") is not True:
                continue
            prefix = "pre_merge_" if record.get("pre_merge") is True else ""
            out[f"{prefix}calls"] += 1
            out[f"{prefix}failed"] += 0 if record.get("ok") else 1
    return out


def merged_tasks(findings_dir: Path | None) -> int | None:
    """How many task directories hold a merged document, or None when it cannot be read.

    None is not zero: an unreadable merge target is no evidence at all, and the
    cost-per-task rule does not fire on it.
    """
    if findings_dir is None:
        return None
    path = Path(findings_dir)
    if not path.is_dir():
        return None
    return sum(1 for child in path.iterdir()
               if child.is_dir() and any(child.glob("*.json")))


def decide(*, dollars: float, max_cost: float, calls: int, failed: int, merged: int | None,
           p90_cost: float | None, p90_gate: float | None, multiple: float,
           min_tasks: int = MIN_TASKS, min_calls: int = MIN_GATE_CALLS) -> list[str]:
    """The reasons to drain now; empty means keep going.

    The two floors are the per-task rule's lesson: a rate over four calls and a cost per
    task over two tasks are noise, and a rule that fires on noise is a rule an operator
    turns off. ``merged`` of None — the merge target could not be read — fires nothing.
    """
    reasons = []
    if max_cost > 0 and dollars >= max_cost:
        reasons.append(f"spend ${dollars:.2f} reached the shard's cap ${max_cost:.2f}")
    if p90_gate is not None and calls >= max(1, min_calls):
        rate = failed / calls
        if rate > multiple * p90_gate:
            reasons.append(f"gate-failure rate {rate:.3f} over {calls} calls is above "
                           f"{multiple:g} x the pilot's p90 {p90_gate:.3f}")
    if p90_cost is not None and merged is not None and merged >= max(1, min_tasks):
        per_task = dollars / merged
        if per_task > multiple * p90_cost:
            reasons.append(f"cost per merged task ${per_task:.2f} over {merged} tasks is "
                           f"above {multiple:g} x the pilot's p90 ${p90_cost:.2f}")
    return reasons


def measure(args) -> dict:
    """One reading of the root: what was spent, what the gate said, what merged."""
    root = Path(args.root)
    spent = ledger(root / "usage", fallback_usd_per_hour=args.unpriced_usd_per_hour)
    gate = gate_counts(Path(args.gate_log) if args.gate_log else root / "gate-log")
    merged = merged_tasks(Path(args.findings) if args.findings else None)
    estimate = spent["estimated_unpriced_usd"]
    total = round(spent["dollars"] + (estimate or 0.0), 6)
    return {"spend_usd": total, "spend_floor_usd": spent["dollars"],
            "unpriced_calls": spent["unpriced_calls"],
            "unpriced_seconds": spent["unpriced_seconds"],
            "estimated_unpriced_usd": estimate,
            "estimate_usd_per_second": spent["usd_per_second"],
            "envelopes": spent["envelopes"], "gate_calls": gate["calls"],
            "gate_failed": gate["failed"], "pre_merge_calls": gate["pre_merge_calls"],
            "pre_merge_failed": gate["pre_merge_failed"], "merged_tasks": merged,
            **({"spend_by_step": spent["by_step"]} if "by_step" in spent else {})}


def _write(root: Path, record: dict) -> None:
    """Append the record and replace ``watchdog.json``; a write it cannot do is lost."""
    try:
        with open(root / "watchdog.jsonl", "a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, sort_keys=True) + "\n")
        tmp = root / "watchdog.json.tmp"
        tmp.write_text(json.dumps(record, indent=1, sort_keys=True) + "\n", encoding="utf-8")
        os.replace(tmp, root / "watchdog.json")
    except OSError:
        pass


def check(args) -> dict:
    """One check: measure, decide, drain if it must, and record what it did."""
    root = Path(args.root)
    reading = measure(args)
    reasons = decide(dollars=reading["spend_usd"], max_cost=args.max_cost,
                     calls=reading["gate_calls"], failed=reading["gate_failed"],
                     merged=reading["merged_tasks"], p90_cost=args.p90_cost_per_task,
                     p90_gate=args.p90_gate_failure_rate, multiple=args.multiple,
                     min_tasks=args.min_tasks, min_calls=args.min_gate_calls)
    try:
        previous = json.loads((root / "watchdog.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        previous = {}
    record = {"at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "root": str(root),
              "max_cost": args.max_cost, "reasons": reasons,
              "drained": bool(previous.get("drained")), "action": None,
              "settled": False, **reading}
    if reasons and not record["drained"]:
        command = args.drain_command.format(root=shlex.quote(str(root)))
        record["action"] = command
        if args.dry_run:
            record["drained"] = True
            record["drain_exit"] = None
            record["drain_output"] = "dry run: nothing was drained"
        else:
            try:
                proc = subprocess.run(shlex.split(command), capture_output=True, text=True,
                                      timeout=300)
                record["drain_exit"] = proc.returncode
                record["drain_output"] = (proc.stdout + proc.stderr).strip()[-500:]
                record["drained"] = proc.returncode == 0
            except (OSError, subprocess.SubprocessError) as exc:
                record["drain_exit"] = None
                record["drain_output"] = str(exc)
    _write(root, record)
    return record


def settle(args, after: dict, *, sleep=time.sleep, now=time.monotonic) -> dict:
    """Keep watching after a drain until the spend stops moving, and say where it ended.

    A drain starts nothing new; the calls in flight keep spending, so the cap is not
    enforced by the drain alone. This watches the root until ``--settle-checks``
    consecutive readings show the same spend and the same envelope count, or
    ``--settle-timeout`` seconds pass, and returns the final record with ``settled``,
    ``spend_after_drain_usd`` and ``over_cap_after_drain``.
    """
    root = Path(args.root)
    deadline = now() + max(0.0, args.settle_timeout)
    last = (after["spend_usd"], after["envelopes"])
    quiet = 0
    reading = after
    while quiet < max(1, args.settle_checks) and now() < deadline:
        sleep(min(max(1.0, args.settle_interval), max(0.0, deadline - now()) or 1.0))
        reading = measure(args)
        here = (reading["spend_usd"], reading["envelopes"])
        quiet = quiet + 1 if here == last else 0
        last = here
    over = args.max_cost > 0 and reading["spend_usd"] > args.max_cost
    record = {"at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "root": str(root),
              "max_cost": args.max_cost, "reasons": after.get("reasons") or [],
              "drained": True, "action": after.get("action"), "settled": quiet > 0,
              "settle_checks_quiet": quiet,
              "spend_after_drain_usd": reading["spend_usd"],
              "spend_at_drain_usd": after["spend_usd"],
              "over_cap_after_drain": bool(over), **reading}
    _write(root, record)
    return record


def wait(args, stop: Path, *, sleep=time.sleep, now=time.monotonic) -> str | None:
    """Sleep one interval, looking every :data:`POLL` seconds at the stop file."""
    deadline = now() + max(1.0, args.interval)
    while True:
        if stop.exists():
            return "stop"
        left = deadline - now()
        if left <= 0:
            return None
        sleep(min(left, POLL))


def _parse(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0], allow_abbrev=False)
    parser.add_argument("--root", required=True, help="the shard's tend root")
    parser.add_argument("--max-cost", type=float, default=None,
                        help="the shard's spend cap from its run configuration, in USD")
    parser.add_argument("--gate-log", help="the gate log, or the directory of them "
                                           "(default <root>/gate-log)")
    parser.add_argument("--findings", help="the merge target's findings directory, for the "
                                           "merged-task count")
    parser.add_argument("--p90-cost-per-task", type=float, default=None)
    parser.add_argument("--p90-gate-failure-rate", type=float, default=None)
    parser.add_argument("--multiple", type=float, default=2.0)
    parser.add_argument("--unpriced-usd-per-hour", type=float, default=None,
                        help="what an hour of a call that left no priced envelope is "
                             "estimated at before any priced envelope with a duration exists")
    parser.add_argument("--min-tasks", type=int, default=MIN_TASKS)
    parser.add_argument("--min-gate-calls", type=int, default=MIN_GATE_CALLS)
    parser.add_argument("--drain-command", default="tend-control drain --root {root}",
                        help="the host's drain command; {root} is the tend root")
    parser.add_argument("--interval", type=float, default=300.0, help="seconds between checks")
    parser.add_argument("--settle-timeout", type=float, default=SETTLE_TIMEOUT)
    parser.add_argument("--settle-checks", type=int, default=SETTLE_CHECKS)
    parser.add_argument("--settle-interval", type=float, default=SETTLE_INTERVAL)
    parser.add_argument("--no-settle", action="store_true",
                        help="exit as soon as the drain returns; the cap is then unenforced "
                             "while the calls in flight finish, and the record says so")
    parser.add_argument("--once", action="store_true", help="check once and exit")
    parser.add_argument("--dry-run", action="store_true",
                        help="never drain; record what it would do")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None, *, sleep=time.sleep) -> int:
    args = _parse(argv)
    if args.max_cost is None:
        print("watchdog: --max-cost is required (a shard needs a recorded cap)",
              file=sys.stderr)
        return EXIT_USAGE
    if args.max_cost <= 0:
        print("watchdog: --max-cost must be positive", file=sys.stderr)
        return EXIT_USAGE
    if args.min_gate_calls < 1 or args.min_tasks < 1:
        print("watchdog: --min-gate-calls and --min-tasks are at least 1", file=sys.stderr)
        return EXIT_USAGE
    stop = Path(args.root) / "watchdog.stop"
    while True:
        record = check(args)
        print(json.dumps(record, sort_keys=True), flush=True)
        if record["drained"]:
            if args.no_settle:
                return EXIT_DRAINED
            final = settle(args, record, sleep=sleep)
            print(json.dumps(final, sort_keys=True), flush=True)
            if final["over_cap_after_drain"]:
                print(f"watchdog: the drain left ${final['spend_after_drain_usd']:.2f} "
                      f"spent against a cap of ${args.max_cost:.2f}", file=sys.stderr)
                return EXIT_OVER_CAP
            return EXIT_DRAINED
        if args.once or stop.exists():
            return EXIT_OK
        if wait(args, stop, sleep=sleep) == "stop":
            return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
