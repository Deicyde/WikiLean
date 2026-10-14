#!/usr/bin/env python3
"""Read-only status of the @[wikidata] poller for dashboards (meant for manage/status.py).

    gate_status.py            local only: main's frozen snapshot, no network
    gate_status.py --live     also read GitHub (read-only gh calls): the live state
                              on the bot-state branch, recent poller runs on main,
                              and the batch PR

Always exits 0 and prints one JSON object. Anything it could not read, or did not
check, is reported in `alerts`, never dropped: a dashboard that hid the poller's
failures is how a dead token went unnoticed for three weeks. With --live it makes
at most five gh calls of up to TIMEOUT_S each; callers should allow 90 s.

It never runs the poller's decide(): that would use the operator's own gh login,
not the CI token, and so cannot see the failure that matters. It reports what the
CI poller itself concluded instead.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
SNAPSHOT = HERE / "state" / "bot_state.json"
REPO = "Deicyde/WikiLean"
UPSTREAM = "leanprover-community/mathlib4"
WORKFLOW = "wikidata-poll.yml"
STATE_BRANCH = "bot-state"
TIMEOUT_S = 15
# GitHub delivers this repo's schedules hours late (about every 4 h for a */30
# cron), so liveness allows a day without a successful run.
WINDOW_H = 24
PR_OPEN_ALERT_DAYS = 30


def gh(args):
    """stdout of a read-only `gh` call; RuntimeError with gh's reason on failure."""
    try:
        r = subprocess.run(["gh", *args], capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=TIMEOUT_S)
    except (OSError, subprocess.TimeoutExpired) as ex:
        raise RuntimeError(f"gh {args[0]}: {ex}") from ex
    if r.returncode != 0:
        detail = " ".join((r.stderr or r.stdout or "no output").split())[:200]
        raise RuntimeError(f"gh {' '.join(args[:2])} failed (exit {r.returncode}): {detail}")
    return r.stdout


def parse_time(value):
    return dt.datetime.fromisoformat(value.replace("Z", "+00:00"))


def as_state(text, source):
    try:
        state = json.loads(text)
    except ValueError as ex:
        return {"source": source, "error": f"unparseable bot_state.json: {ex}"}
    if not isinstance(state, dict):
        return {"source": source, "error": f"bot_state.json is a {type(state).__name__}, not an object"}
    return {**state, "source": source}


def read_snapshot():
    try:
        text = SNAPSHOT.read_text(encoding="utf-8")
    except (OSError, ValueError) as ex:
        return {"source": "main-snapshot", "error": str(ex)}
    return as_state(text, "main-snapshot")


def read_state(live):
    if not live:
        return read_snapshot()
    try:
        return as_state(gh(["api", "-H", "Accept: application/vnd.github.raw",
                            f"repos/{REPO}/contents/bot/state/bot_state.json?ref={STATE_BRANCH}"]),
                        STATE_BRANCH)
    except RuntimeError as ex:
        if "HTTP 404" not in str(ex):
            return {"source": STATE_BRANCH, "error": str(ex)}
    # A 404 is either "no branch" or "branch without the file"; only the first can be benign.
    try:
        gh(["api", f"repos/{REPO}/branches/{STATE_BRANCH}", "--jq", ".name"])
    except RuntimeError as ex:
        if "HTTP 404" not in str(ex):
            return {"source": STATE_BRANCH, "error": str(ex)}
        return {**read_snapshot(), "branch_missing": True}
    return {"source": STATE_BRANCH,
            "error": f"{STATE_BRANCH} has no bot/state/bot_state.json; every poller run fails at restore"}


def decision_of(steps):
    """(decision, first failed step) from a run's step outcomes, without its logs.

    The steps between Decide and Commit poll state run only when Decide said act, so
    whether any of them ran tells act from wait even when one of them failed."""
    names = [s.get("name") or "" for s in steps]
    index = lambda prefix: next((i for i, n in enumerate(names) if n.startswith(prefix)), None)
    failed = next((s.get("name") for s in steps if s.get("conclusion") == "failure"), None)
    decide, commit = index("Decide"), index("Commit poll state")
    if decide is None or commit is None or commit < decide:
        return "unknown", failed
    if steps[decide].get("conclusion") != "success":
        return ("error" if failed else "unknown"), failed
    acted = any(s.get("conclusion") not in ("skipped", None) for s in steps[decide + 1:commit])
    return ("act" if acted else "wait"), failed


def read_poller(now):
    try:
        runs = json.loads(gh(["run", "list", "--repo", REPO, "--workflow", WORKFLOW, "--branch", "main",
                              "--limit", "40", "--json",
                              "databaseId,status,conclusion,createdAt,updatedAt,event,url"]))
    except (RuntimeError, ValueError) as ex:
        return {"error": f"cannot list poller runs: {ex}"}
    if not isinstance(runs, list):
        return {"error": "cannot list poller runs: unexpected output"}
    # A run the concurrency group cancelled before it started says nothing about the poller.
    done = [r for r in runs if isinstance(r, dict) and r.get("status") == "completed"
            and r.get("conclusion") not in ("cancelled", "skipped")]
    if not done:
        return {"error": "no completed poller runs on main"}
    latest = done[0]
    out = {"run_id": latest.get("databaseId"), "url": latest.get("url"), "event": latest.get("event"),
           "created_at": latest.get("createdAt"), "conclusion": latest.get("conclusion")}
    ok = next((r for r in done if r.get("conclusion") == "success"), None)
    out["last_success_at"] = (ok.get("updatedAt") or ok.get("createdAt")) if ok else None
    recent_red = [r for r in done if r.get("conclusion") != "success" and r.get("createdAt")
                  and (now - parse_time(r["createdAt"])).total_seconds() <= WINDOW_H * 3600]
    out["failed_last_24h"] = len(recent_red)
    if recent_red:
        out["latest_failure_url"] = recent_red[0].get("url")
    try:
        jobs = json.loads(gh(["api", f"repos/{REPO}/actions/runs/{latest.get('databaseId')}/jobs"]))
        steps = next((j.get("steps") or [] for j in jobs.get("jobs", []) if j.get("name") == "poll"), [])
        out["decision"], out["failed_step"] = decision_of(steps)
    except (RuntimeError, ValueError, AttributeError) as ex:
        out["decision_error"] = str(ex)
    return out


def read_pr(number):
    try:
        pr = json.loads(gh(["pr", "view", str(number), "--repo", UPSTREAM, "--json",
                            "state,mergeable,createdAt,labels,url"]))
    except (RuntimeError, ValueError) as ex:
        return {"number": number, "error": str(ex)}
    if not isinstance(pr, dict):
        return {"number": number, "error": "unexpected gh pr view output"}
    return {"number": number, "state": pr.get("state"), "mergeable": pr.get("mergeable"),
            "created_at": pr.get("createdAt"), "url": pr.get("url"),
            "labels": sorted(l.get("name", "") for l in pr.get("labels") or [] if isinstance(l, dict))}


def alerts_for(report, now):
    alerts = []
    state = report["state"]
    if "error" in state:
        alerts.append(f"bot state unreadable ({state['source']}): {state['error']}")
    elif state.get("branch_missing"):
        alerts.append(f"no {STATE_BRANCH} branch: showing main's frozen snapshot. Expected only before the "
                      f"one-time bootstrap_state dispatch; after that the branch was deleted or renamed, and "
                      f"every poller run fails at restore until it is recreated (bot/README.md, Restore failed)")
    if not report["live"]:
        alerts.append("offline: main's frozen snapshot only; poller runs and the batch PR were not checked "
                      "(use --live)")
    poller = report.get("poller")
    if poller is not None:
        if "error" in poller:
            alerts.append(f"poller runs unreadable: {poller['error']}")
        else:
            if poller.get("conclusion") != "success":
                failed = f", failed at '{poller['failed_step']}'" if poller.get("failed_step") else ""
                alerts.append(f"latest poller run {poller['run_id']} is {poller.get('conclusion')} "
                              f"(decision {poller.get('decision', 'unknown')}{failed}): {poller.get('url')}")
            elif poller.get("failed_last_24h"):
                alerts.append(f"{poller['failed_last_24h']} poller run(s) failed in the last {WINDOW_H}h "
                              f"(latest: {poller.get('latest_failure_url')})")
            last = poller.get("last_success_at")
            if not last or (now - parse_time(last)).total_seconds() > WINDOW_H * 3600:
                alerts.append(f"no successful poller run in {WINDOW_H}h (last: {last})")
    pr = report.get("pr")
    if pr is not None:
        stale = "" if state.get("source") == STATE_BRANCH else " (batch number from main's frozen snapshot)"
        if "error" in pr:
            alerts.append(f"batch PR #{pr['number']} unreadable{stale}: {pr['error']}")
        elif pr.get("state") == "OPEN":
            # GitHub answers UNKNOWN while it recomputes; Mathlib's label is steadier.
            if pr.get("mergeable") == "CONFLICTING" or "merge-conflict" in pr.get("labels", []):
                alerts.append(f"batch PR #{pr['number']} conflicts with master{stale}")
            if pr.get("created_at"):
                days = (now - parse_time(pr["created_at"])).days
                if days > PR_OPEN_ALERT_DAYS:
                    alerts.append(f"batch PR #{pr['number']} open {days} days{stale}")
        elif pr.get("state") == "CLOSED":
            alerts.append(f"batch PR #{pr['number']} is CLOSED, not merged; the poller halts until a "
                          f"human decides{stale}")
    return alerts


def report(live, now=None):
    now = now or dt.datetime.now(dt.timezone.utc)
    out = {"live": live, "generated_at": now.isoformat(timespec="seconds"), "state": read_state(live)}
    if live:
        out["poller"] = read_poller(now)
        pr = out["state"].get("current_pr") if "error" not in out["state"] else None
        if pr:
            out["pr"] = read_pr(pr)
    out["alerts"] = alerts_for(out, now)
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--live", action="store_true", help="also read GitHub (gh, read-only)")
    args = ap.parse_args()
    try:
        out = report(args.live)
    except Exception as ex:  # the contract is one JSON object, whatever happened
        out = {"live": args.live, "alerts": [f"gate_status failed: {type(ex).__name__}: {ex}"]}
    json.dump(out, sys.stdout, indent=1, sort_keys=True)
    sys.stdout.write("\n")


if __name__ == "__main__":
    main()
