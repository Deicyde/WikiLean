#!/usr/bin/env python3
"""Ground-truth snapshot — the session-start read of the control plane.

Prints where WikiLean actually is right now, so a session opens from truth
instead of a stale handoff: coverage, the Agent-2 backlog, worklist depths, tag
pool runway, graph freshness, the live @[wikidata] batch, and uncommitted git
state — then the decisions that are waiting on a human.

Offline and instant by default (reads manage/data/digest.json + local state).
``--live`` adds network checks: the site returns 200; the bot gate, both from a
local ``poll.py --decide`` and from the latest CI poll run's Decide step; the
batch PR's merge state and age; the live Brain release; and nightly review
health from the /stats pipeline-runs table, compared with the previous --live
reading (kept in site/cache/.status_live.json). ``--refresh`` regenerates the
digest first (see refresh.py).

    python3 manage/status.py
    python3 manage/status.py --live
    python3 manage/status.py --refresh --live
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
HERE = Path(__file__).resolve().parent
DATA = HERE / "data"
DIGEST = DATA / "digest.json"
BOT_STATE = ROOT / "bot" / "state" / "bot_state.json"
SITE_URL = "https://wikilean.jackmccarthy.org"
POLL_REPO = "Deicyde/WikiLean"
POLL_WORKFLOW = "wikidata-poll.yml"
MATHLIB_REPO = "leanprover-community/mathlib4"
LIVE_STATE = ROOT / "site" / "cache" / ".status_live.json"  # gitignored

# Rule-of-thumb thresholds (see HANDOFF §4).
LOW_POOL_BATCHES = 3.0     # generate more tags below this
STALE_DIGEST_HOURS = 36    # nightly refresh should keep it under a day
BATCH_PR_STALE_DAYS = 30   # a batch PR open longer than this needs a nudge
# One bulk copy of the catalog and annotations leaves their mtimes seconds apart
# (4 s on 2026-08-19); only a gap longer than this means the graph is stale.
GRAPH_STALE_SLACK_S = 300


def graph_older_than_annotations(graph_mtime: float, ann_mtime: float) -> bool:
    return graph_mtime + GRAPH_STALE_SLACK_S < ann_mtime


def _age(ts: float) -> str:
    if not ts:
        return "?"
    h = (time.time() - ts) / 3600
    return f"{h:.0f}h ago" if h >= 1 else f"{h * 60:.0f}m ago"


def _run(cmd: list[str], timeout: int = 12) -> str | None:
    try:
        r = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True, timeout=timeout)
        return r.stdout.strip() if r.returncode == 0 else None
    except Exception:
        return None


def _fetch(url: str, timeout: int = 15) -> str | None:
    """GET via curl (system trust store); None on any failure or HTTP error."""
    return _run(["curl", "-sS", "-f", "-m", str(timeout), url], timeout=timeout + 5)


def _days_since(iso: str) -> float | None:
    try:
        t = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    except (AttributeError, ValueError):
        return None
    if t.tzinfo is None:
        t = t.replace(tzinfo=timezone.utc)
    return (time.time() - t.timestamp()) / 86400


def parse_pipeline_runs(html: str) -> dict:
    """{kind: {runs, articles, errors, tokens}} from the /stats Pipeline runs table."""
    m = re.search(r"Pipeline runs</h2>(.*?)</table>", html or "", re.S)
    if not m:
        return {}
    num = r"<td[^>]*>([\d,]+)</td>"
    out = {}
    for kind, *vals in re.findall(r"<tr><td>([^<]+)</td>" + num * 4, m.group(1)):
        if kind != "all runs":
            out[kind] = dict(zip(("runs", "articles", "errors", "tokens"),
                                 (int(v.replace(",", "")) for v in vals)))
    return out


def review_alerts(prev: dict | None, last: dict | None) -> list[str]:
    """Alerts from the review totals between two readings with new runs."""
    if not prev or not last or last["runs"] <= prev["runs"]:
        return []
    runs = last["runs"] - prev["runs"]
    arts = last["articles"] - prev["articles"]
    errs = last["errors"] - prev["errors"]
    toks = last["tokens"] - prev["tokens"]
    found = []
    if arts > 0 and errs == arts:
        found.append(f"errors equal articles ({errs}/{arts})")
    if arts > 0 and toks == 0:
        found.append("tokens unchanged")
    if not found:
        return []
    since = prev.get("at") or "?"
    return [f"nightly review failing: {runs} run(s) since {since}, "
            f"{' and '.join(found)}; see docs/OPERATIONS.md"]


def review_health(stats_html: str | None, now_iso: str,
                  state_path: Path | None = None) -> tuple[str, list[str]]:
    """Compare the /stats review row with the stored readings. The state keeps
    the last reading and the one before the last change, so an alert persists
    until the next run changes the totals."""
    review = parse_pipeline_runs(stats_html or "").get("review")
    if review is None:
        return "  review     /stats unavailable", []
    path = state_path or LIVE_STATE
    try:
        state = json.loads(path.read_text())
    except (OSError, ValueError):
        state = {}
    last = state.get("last")
    cur = {**review, "at": now_iso}
    note = ""
    if not last or review["runs"] < last["runs"]:
        state = {"prev": None, "last": cur}
        note = "  (baseline saved; compared from the next nightly run)"
    elif any(review[k] != last[k] for k in review):
        state = {"prev": last, "last": cur}
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(state, indent=1))
    except OSError:
        pass
    line = (f"  review     {review['runs']} runs · {review['articles']:,} articles · "
            f"{review['errors']:,} errors · {review['tokens']:,} tokens{note}")
    return line, review_alerts(state.get("prev"), state.get("last"))


def _decision(text: str | None) -> str | None:
    m = re.findall(r"POLL_DECISION=(act|wait)\s*$", text or "", re.M)
    return m[-1] if m else None


def ci_gate() -> tuple[str | None, str]:
    """The latest completed wikidata-poll run's Decide output, via gh. CI runs
    decide with MATHLIB_FORK_PAT, which a local decide (the operator's gh
    login) cannot see failing."""
    out = _run(["gh", "run", "list", "--repo", POLL_REPO, "--workflow", POLL_WORKFLOW,
                "--status", "completed", "-L", "1", "--json", "databaseId,createdAt"],
               timeout=30)
    try:
        run = json.loads(out)[0]
    except (TypeError, ValueError, IndexError, KeyError):
        return None, "CI ?"
    log = _run(["gh", "run", "view", str(run["databaseId"]), "--repo", POLL_REPO, "--log"],
               timeout=60)
    age = _days_since(run.get("createdAt", ""))
    when = f"{age * 24:.0f}h ago" if age is not None else "?"
    dec = _decision(log)
    return dec, f"CI {dec or '?'} (run {run['databaseId']}, {when})"


def batch_pr(pr) -> tuple[str, list[str]]:
    out = _run(["gh", "pr", "view", str(pr), "--repo", MATHLIB_REPO,
                "--json", "state,mergeable,createdAt,labels"], timeout=30)
    try:
        d = json.loads(out)
    except (TypeError, ValueError):
        return f"  batch PR   #{pr}: gh unavailable", []
    age = _days_since(d.get("createdAt", ""))
    alerts = []
    # GitHub often answers UNKNOWN while it recomputes mergeability; mathlib's
    # bot-managed merge-conflict label is the steadier signal.
    labels = {lab.get("name") for lab in d.get("labels") or [] if isinstance(lab, dict)}
    if d.get("mergeable") == "CONFLICTING" or "merge-conflict" in labels:
        alerts.append(f"batch PR #{pr} conflicts with mathlib master; rebuild it off master")
    if d.get("state") == "OPEN" and age is not None and age > BATCH_PR_STALE_DAYS:
        alerts.append(f"batch PR #{pr} has been open {age:.0f} days; find a maintainer to merge it")
    age_s = f"{age:.0f}d" if age is not None else "?"
    return (f"  batch PR   #{pr} {d.get('state', '?')} · {d.get('mergeable', '?')} "
            f"· opened {age_s} ago"), alerts


def brain_release() -> str:
    try:
        sel = json.loads(_fetch(f"{SITE_URL}/assets/brain/current.json") or "")
    except ValueError:
        sel = None
    if not isinstance(sel, dict):
        return "  brain      /assets/brain/current.json unavailable"
    audited = sel.get("audited_at") or "?"
    age = _days_since(audited)
    age_s = f" ({age:.0f}d ago)" if age is not None else ""
    return f"  brain      release {sel.get('release_id', '?')}  audited {audited}{age_s}"


def live_health(batch: dict) -> tuple[list[str], list[str]]:
    """Live lines and alerts that need no digest."""
    lines, alerts = [], []
    line, a = review_health(_fetch(f"{SITE_URL}/stats"),
                            datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%MZ"))
    lines.append(line)
    alerts += a
    lines.append(brain_release())
    if batch.get("current_pr"):
        line, a = batch_pr(batch["current_pr"])
        lines.append(line)
        alerts += a
    return lines, alerts


def gate_line(local: str | None) -> tuple[str, list[str]]:
    dec_local = _decision(local)
    dec_ci, ci = ci_gate()
    alerts = []
    if dec_local == "act" and dec_ci == "wait":
        alerts.append("bot gate: CI decided 'wait' where a local decide says 'act': "
                      "the poller's MATHLIB_FORK_PAT may be failing; read the "
                      "wikidata-poll Decide step")
    return f"gate: local {dec_local or 'timeout/error'} · {ci}", alerts


def snapshot(live: bool = False) -> None:
    batch = json.loads(BOT_STATE.read_text()) if BOT_STATE.exists() else {}
    if not DIGEST.exists():
        print("no digest yet — run `python3 manage/refresh.py` first")
        if live:
            lines, alerts = live_health(batch)
            for line in lines:
                print(line)
            for a in alerts:
                print(f"    → {a}")
        return
    d = json.loads(DIGEST.read_text())
    c, w, pool, fresh = d["coverage"], d["worklists"], d["pool"], d["freshness"]

    print("═" * 64)
    print(f"  WikiLean status   (digest {_age(d.get('generated_at', 0))})")
    print("═" * 64)

    print(f"  coverage   {c['pct_formalized']}% formalized / {c['pct_partial']}% partial "
          f"/ {c['pct_not_formalized']}% not   ({c['n_articles']} articles)")
    print(f"  graph      {d['graph']['n_nodes']} concepts, {d['graph']['n_edges']} edges")

    # @[wikidata] batch (offline from bot state; live gate with --live)
    gate_alerts: list[str] = []
    gate_s = ""
    if live:
        local = _run([sys.executable, "bot/poll.py", "--mathlib", "/tmp/unused", "--decide"],
                     timeout=30)
        g, gate_alerts = gate_line(local)
        gate_s = f"  {g}"
    if batch:
        print(f"  pipeline   batch {batch.get('batch_num')} · PR #{batch.get('current_pr')}"
              f" · pool {pool['fresh_candidates']} candidates (~{pool['approx_batches']} batches){gate_s}")

    print()
    print("  work waiting (from the coverage graph):")
    print(f"    formalize     {w['formalize']['total']:>4}  articles with extracted statements awaiting Agent 2")
    print(f"    annotate      {w['annotate']['total']:>4}  central concepts needing a first pass")
    print(f"    coverage map  {w['coverage_gaps']['total']:>4}  moderated articles, low coverage (informational)")
    for r in w["formalize"]["top"][:3]:
        print(f"      · {r['label']} ({r['n_extracted']} stmts, cen {r['centrality_pct']:.0f})")

    # Live site check
    live_alerts: list[str] = []
    if live:
        code = _run(["curl", "-s", "-o", "/dev/null", "-w", "%{http_code}", "-m", "8", SITE_URL])
        print(f"\n  site       {SITE_URL} → {code or 'unreachable'}")
        lines, live_alerts = live_health(batch)
        for line in lines:
            print(line)

    # Decisions waiting on a human
    alerts = gate_alerts + live_alerts
    stale_graph = (graph_older_than_annotations(fresh["graph_mtime"], fresh["annotations_mtime"])
                   if "graph_mtime" in fresh and "annotations_mtime" in fresh
                   else fresh.get("graph_older_than_annotations"))
    if stale_graph:
        alerts.append("concept graph is stale vs the annotation layer — rebuild (catalog/mathlib_deps/merge_graph.py)")
    if pool["approx_batches"] < LOW_POOL_BATCHES:
        alerts.append(f"tag pool low (~{pool['approx_batches']} batches) — run a generation pass")
    if c["extracted_articles"]:
        alerts.append(f"{c['extracted_articles']} articles / {c['extracted_statements']} statements await Agent-2 formalization")
    dirty = _run(["git", "status", "--porcelain"])
    if dirty:
        n = len(dirty.splitlines())
        alerts.append(f"{n} uncommitted file(s) in the working tree")
    if (time.time() - d.get("generated_at", 0)) / 3600 > STALE_DIGEST_HOURS:
        alerts.append(
            "digest is stale — run `python3 manage/refresh.py` (or add "
            "`--snapshot-bundle /absolute/path` to refresh the D1 mirror)"
        )

    print("\n  decisions waiting:" if alerts else "\n  nothing waiting.")
    for a in alerts:
        print(f"    → {a}")
    print("═" * 64)


def main() -> None:
    args = sys.argv[1:]
    if "--refresh" in args:
        subprocess.run([sys.executable, str(HERE / "refresh.py")]
                       + (["--pull"] if "--pull" in args else []), cwd=ROOT)
    snapshot(live="--live" in args)


if __name__ == "__main__":
    main()
