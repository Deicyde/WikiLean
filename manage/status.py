#!/usr/bin/env python3
"""Ground-truth snapshot — the session-start read of the control plane.

Prints where WikiLean actually is right now, so a session opens from truth
instead of a stale handoff: coverage, the Agent-2 backlog, worklist depths, tag
pool runway, graph freshness, the live @[wikidata] batch, and uncommitted git
state — then the decisions that are waiting on a human.

Offline and instant by default (reads manage/data/digest.json + local state).
``--live`` adds read-only network checks: the site returns 200; the bot gate
from the latest CI poll run; the batch PR's merge state and age; nightly
review health (per run from GET /api/runs, or from the /stats totals compared
with the previous --live reading in site/cache/.status_live.json where that
endpoint is not deployed); and the live Brain release. ``--refresh``
regenerates the digest first (see refresh.py).

    python3 manage/status.py
    python3 manage/status.py --live
    python3 manage/status.py --refresh --live
"""
from __future__ import annotations

import base64
import json
import os
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
POLL_STALE_HOURS = 12      # the poll cron completes a run every few hours in practice
POLL_RUNS_LISTED = 20      # runs read per check; skipped and early-cancelled ones are passed over
# A run cancelled by the job's 60-minute limit in wikidata-poll.yml measures at least
# 60 minutes, since durations run from startedAt, which includes queue time. A
# cancelled run shorter than this is a queued run the concurrency group dropped, or a
# manual cancel, never a timeout; 55 leaves margin, and a 55-60 minute cancel may still
# be either. (Under the old 30-minute limit, 209 of the poller's 210 cancelled runs to
# 2026-10 lasted 30 minutes or more.)
POLL_TIMEOUT_MINUTES = 55
REVIEW_STALE_HOURS = 36    # the review runs once a night
# One bulk copy of the catalog and annotations leaves their mtimes seconds apart
# (4 s on 2026-08-19); only a gap longer than this means the graph is stale.
GRAPH_STALE_SLACK_S = 300
RUN_KEYS = ("runs", "articles", "errors", "tokens")


def graph_older_than_annotations(graph_mtime: float, ann_mtime: float) -> bool:
    return graph_mtime + GRAPH_STALE_SLACK_S < ann_mtime


def _age(ts: float) -> str:
    if not ts:
        return "?"
    h = (time.time() - ts) / 3600
    return f"{h:.0f}h ago" if h >= 1 else f"{h * 60:.0f}m ago"


def _run_err(cmd: list[str], timeout: int = 12) -> tuple[str | None, str]:
    """(stdout or None on failure, stderr), so a caller can tell why a read failed."""
    try:
        r = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True, timeout=timeout)
        return (r.stdout.strip() if r.returncode == 0 else None), r.stderr.strip()
    except Exception as e:
        return None, str(e)


def _run(cmd: list[str], timeout: int = 12) -> str | None:
    return _run_err(cmd, timeout)[0]


def _first_line(err: str) -> str:
    return (err.strip().splitlines() or ["no error output"])[0][:160]


def _fetch(url: str, timeout: int = 15) -> str | None:
    """GET via curl (system trust store); None on any failure or HTTP error."""
    return _run(["curl", "-sS", "-f", "-m", str(timeout), url], timeout=timeout + 5)


def _hours_since(iso: str) -> float | None:
    try:
        t = datetime.fromisoformat(str(iso).replace("Z", "+00:00"))
    except ValueError:
        return None
    if t.tzinfo is None:
        t = t.replace(tzinfo=timezone.utc)
    return (time.time() - t.timestamp()) / 3600


# ---------------------------------------------------------------------------
# Nightly review health
# ---------------------------------------------------------------------------

def _failing(articles: int, errors: int, tokens: int) -> list[str]:
    found = []
    if articles > 0 and errors == articles:
        found.append(f"errors equal articles ({errors}/{articles})")
    if articles > 0 and tokens == 0:
        found.append("tokens unchanged")
    return found


def review_from_runs(runs: list, now: float) -> tuple[list[str], list[str]]:
    """Per-run review health from GET /api/runs (newest first)."""
    if not runs:
        return ["  review     no review runs reported"], ["no review run has been reported"]
    newest = runs[0]
    age_h = (now - (newest.get("finished_at") or 0) / 1000) / 3600
    lines = [f"  review     last run {age_h:.0f}h ago: {newest.get('articles_processed', 0)} "
             f"articles · {newest.get('errors', 0)} errors · {newest.get('tokens', 0):,} tokens"]
    alerts = []
    streak = 0
    for r in runs:
        if not _failing(r.get("articles_processed") or 0, r.get("errors") or 0,
                        r.get("tokens") or 0):
            break
        streak += 1
    if streak:
        why = _failing(newest.get("articles_processed") or 0, newest.get("errors") or 0,
                       newest.get("tokens") or 0)
        alerts.append(f"nightly review failing: the last {streak} run(s) had "
                      f"{' and '.join(why)}; see docs/OPERATIONS.md")
        if newest.get("notes"):
            lines.append(f"             notes: {str(newest['notes'])[:300]}")
    if age_h > REVIEW_STALE_HOURS:
        alerts.append(f"no review run has finished in {age_h:.0f}h")
    return lines, alerts


def parse_pipeline_runs(html: str) -> dict:
    """{kind: {runs, articles, errors, tokens}} from the /stats Pipeline runs table."""
    m = re.search(r"Pipeline runs</h2>(.*?)</table>", html or "", re.S)
    if not m:
        return {}
    num = r"<td[^>]*>([\d,]+)</td>"
    out = {}
    for kind, *vals in re.findall(r"<tr><td>([^<]+)</td>" + num * 4, m.group(1)):
        if kind != "all runs":
            out[kind] = dict(zip(RUN_KEYS, (int(v.replace(",", "")) for v in vals)))
    return out


def review_alerts(prev: dict | None, last: dict | None) -> list[str]:
    """Alerts from the review totals between two readings with new runs."""
    if not prev or not last or last["runs"] <= prev["runs"]:
        return []
    found = _failing(last["articles"] - prev["articles"], last["errors"] - prev["errors"],
                     last["tokens"] - prev["tokens"])
    if not found:
        return []
    return [f"nightly review failing: {last['runs'] - prev['runs']} run(s) since "
            f"{prev.get('at') or '?'}, {' and '.join(found)}; see docs/OPERATIONS.md"]


def _load_state(path: Path) -> dict:
    def ok(r):
        return isinstance(r, dict) and all(
            isinstance(r.get(k), int) and not isinstance(r.get(k), bool) for k in RUN_KEYS)
    try:
        st = json.loads(path.read_text())
    except (OSError, ValueError):
        return {}
    if not isinstance(st, dict):
        return {}
    return {k: st.get(k) if ok(st.get(k)) else None for k in ("prev", "last")}


def _save_state(path: Path, state: dict) -> bool:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
        tmp.write_text(json.dumps(state, indent=1))
        os.replace(tmp, path)
        return True
    except OSError:
        return False


def review_health(stats_html: str | None, now: float,
                  state_path: Path | None = None) -> tuple[list[str], list[str]]:
    """Fallback without GET /api/runs: compare the /stats review totals with
    stored readings. The state keeps the last reading and the one before the
    last change, so an alert persists until the next run changes the totals.
    It cannot see a failing night between two healthy ones."""
    if stats_html is None:
        return ["  review     /stats unavailable"], []
    review = parse_pipeline_runs(stats_html).get("review")
    if review is None:
        return (["  review     no review row in the /stats Pipeline runs table"],
                ["could not read the review row on /stats; parse_pipeline_runs "
                 "needs updating for the page's markup"])
    path = state_path or LIVE_STATE
    state = _load_state(path)
    last = state.get("last")
    cur = {**review, "at": datetime.fromtimestamp(now, timezone.utc).strftime("%Y-%m-%d %H:%MZ"),
           "at_ts": now}
    note, alerts = "", []
    if not last or review["runs"] < last["runs"]:
        state = {"prev": None, "last": cur}
        note = ("  (baseline saved; compared from the next nightly run)"
                if _save_state(path, state) else "  (could not save a baseline)")
    elif any(review[k] != last[k] for k in RUN_KEYS):
        state = {"prev": last, "last": cur}
        _save_state(path, state)
    elif isinstance(last.get("at_ts"), (int, float)) and \
            (now - last["at_ts"]) / 3600 > REVIEW_STALE_HOURS:
        alerts.append(f"no new review run since {last.get('at')}")
    line = (f"  review     {review['runs']} runs · {review['articles']:,} articles · "
            f"{review['errors']:,} errors · {review['tokens']:,} tokens{note}")
    return [line], alerts + review_alerts(state.get("prev"), state.get("last"))


def review_section(now: float) -> tuple[list[str], list[str]]:
    try:
        runs = json.loads(_fetch(f"{SITE_URL}/api/runs?kind=review&limit=10") or "")["runs"]
    except (ValueError, TypeError, KeyError):
        runs = None
    if isinstance(runs, list):
        return review_from_runs(runs, now)
    return review_health(_fetch(f"{SITE_URL}/stats"), now)


# ---------------------------------------------------------------------------
# Bot gate and batch PR
# ---------------------------------------------------------------------------

def _decision(text: str | None) -> str | None:
    m = re.findall(r"POLL_DECISION=(act|wait)\s*$", text or "", re.M)
    return m[-1] if m else None


def _minutes(run: dict) -> float | None:
    """How long a run took: startedAt (when this attempt was queued) to updatedAt."""
    try:
        start, end = (datetime.fromisoformat(str(run.get(k) or "").replace("Z", "+00:00"))
                      for k in ("startedAt", "updatedAt"))
        return (end - start).total_seconds() / 60
    except (ValueError, TypeError):
        return None


def _timed_out(run: dict) -> bool:
    return run.get("conclusion") == "cancelled" and (_minutes(run) or 0) >= POLL_TIMEOUT_MINUTES


def ci_gate() -> tuple[str | None, str, list[str]]:
    """The latest finished wikidata-poll run on main, via gh. Only CI's decision
    is shown: a local decide reads this checkout's copy of the bot state, which
    the poller stops updating once it keeps state on the bot-state branch.
    Skipped runs and short cancelled ones are passed over; a run cancelled after
    POLL_TIMEOUT_MINUTES timed out and counts as a failed run."""
    out, err = _run_err(["gh", "run", "list", "--repo", POLL_REPO, "--workflow", POLL_WORKFLOW,
                         "--branch", "main", "--status", "completed", "-L", str(POLL_RUNS_LISTED),
                         "--json", "databaseId,createdAt,startedAt,updatedAt,conclusion"],
                        timeout=30)
    try:
        runs = json.loads(out)
    except (TypeError, ValueError):
        return None, "CI ?", [f"could not list wikidata-poll runs ({_first_line(err)}); "
                              f"the gate is unknown"]
    run = next((r for r in runs if r.get("conclusion") not in ("cancelled", "skipped")
                or _timed_out(r)), None)
    if run is None:
        why = (f"the last {len(runs)} were all skipped or cancelled early" if runs
               else "gh listed none")
        return None, "CI ?", [f"no finished wikidata-poll run on main ({why}); "
                              f"the gate is unknown"]
    age = _hours_since(run.get("createdAt", ""))
    alerts = []
    if age is not None and age > POLL_STALE_HOURS:
        alerts.append(f"the wikidata poller has not finished a run in {age:.0f}h; "
                      f"check that the workflow is enabled and has Actions minutes")
    log, log_err = _run_err(["gh", "run", "view", str(run["databaseId"]), "--repo", POLL_REPO,
                             "--log"], timeout=60)
    if log is None:
        alerts.append(f"could not read the log of wikidata-poll run {run['databaseId']} "
                      f"({_first_line(log_err)}); its decision is unknown")
    # POLL_ERROR can come from any poll.py step (decide, apply, refresh-table).
    err = re.search(r"Z POLL_ERROR: (.*)$", log or "", re.M)
    dec = _decision(log)
    if err:
        dec = dec or "error"
        alerts.append(f"wikidata-poll run {run['databaseId']} reported POLL_ERROR: "
                      f"{err.group(1)[:160]}")
    elif _timed_out(run):
        alerts.append(f"wikidata-poll run {run['databaseId']} timed out (cancelled after "
                      f"{_minutes(run):.0f} min)")
    elif run.get("conclusion") != "success":
        alerts.append(f"wikidata-poll run {run['databaseId']} ended '{run.get('conclusion')}'")
    when = f"{age:.0f}h ago" if age is not None else "?"
    return dec, f"CI {dec or '?'} (run {run['databaseId']}, {when})", alerts


def gate_section() -> tuple[list[str], list[str]]:
    _, ci, alerts = ci_gate()
    return [f"  gate       {ci}"], alerts


def ci_bot_state() -> tuple[dict | None, str | None]:
    """The poller's live state from the bot-state branch: (state, None);
    (None, None) when there is no such branch (HTTP 404); (None, reason) when
    the read failed for any other reason, a branch without the file included."""
    out, err = _run_err(["gh", "api",
                         f"repos/{POLL_REPO}/contents/bot/state/bot_state.json?ref=bot-state",
                         "--jq", ".content"], timeout=30)
    if out is None:
        if "(HTTP 404)" not in err:
            return None, _first_line(err)
        # The 404 is either no branch or a branch without the file: ask which.
        name, err = _run_err(["gh", "api", f"repos/{POLL_REPO}/branches/bot-state",
                              "--jq", ".name"], timeout=30)
        if name is not None:
            return None, ("the branch has no bot/state/bot_state.json, so every poller run "
                          "fails at restore; see bot/README.md, 'Restore failed'")
        return None, (None if "(HTTP 404)" in err else _first_line(err))
    try:
        st = json.loads(base64.b64decode(out))
    except (ValueError, TypeError):
        st = None
    return (st, None) if isinstance(st, dict) else (None, "bot_state.json is not a JSON object")


def batch_pr(pr, source: str = "local") -> tuple[list[str], list[str]]:
    out, err = _run_err(["gh", "pr", "view", str(pr), "--repo", MATHLIB_REPO,
                         "--json", "state,mergeable,createdAt,labels,title"], timeout=30)
    try:
        d = json.loads(out)
    except (TypeError, ValueError):
        d = None
    if not isinstance(d, dict):
        return ([f"  batch PR   #{pr} ? (could not be read)"],
                [f"could not read batch PR #{pr} ({_first_line(err)}); its merge state is unknown"])
    state = d.get("state", "?")
    # mathlib merges through Bors, which closes the PR (mergedAt stays null) and
    # prefixes its title "[Merged by Bors] - "; only the merge button gives MERGED.
    if state == "CLOSED" and str(d.get("title") or "").startswith("[Merged by Bors]"):
        state = "MERGED (Bors)"
    age = _hours_since(d.get("createdAt", ""))
    days = age / 24 if age is not None else None
    alerts = []
    # GitHub often answers UNKNOWN while it recomputes mergeability; mathlib's
    # bot-managed merge-conflict label is the steadier signal.
    labels = {lab.get("name") for lab in d.get("labels") or [] if isinstance(lab, dict)}
    if state == "OPEN":
        if d.get("mergeable") == "CONFLICTING" or "merge-conflict" in labels:
            alerts.append(f"batch PR #{pr} conflicts with mathlib master; the poller rebuilds "
                          f"a batch off master only once it is settled, so step in if it "
                          f"still conflicts after the settle")
        if days is not None and days > BATCH_PR_STALE_DAYS:
            alerts.append(f"batch PR #{pr} has been open {days:.0f} days; find a maintainer to merge it")
    elif state == "CLOSED":
        alerts.append(f"batch PR #{pr} was closed without merging; the poller waits for a human")
    age_s = f"{days:.0f}d" if days is not None else "?"
    return [f"  batch PR   #{pr} {state} · {d.get('mergeable', '?')} · opened {age_s} ago "
            f"(state: {source})"], alerts


# ---------------------------------------------------------------------------

def brain_release() -> str:
    try:
        sel = json.loads(_fetch(f"{SITE_URL}/assets/brain/current.json") or "")
    except ValueError:
        sel = None
    if not isinstance(sel, dict):
        return "  brain      /assets/brain/current.json unavailable"
    audited = sel.get("audited_at") or "?"
    age = _hours_since(audited)
    age_s = f" ({age / 24:.0f}d ago)" if age is not None else ""
    return f"  brain      release {sel.get('release_id', '?')}  audited {audited}{age_s}"


def live_section(batch: dict) -> tuple[list[str], list[str]]:
    """Every --live line and alert. Read-only: GETs and gh reads."""
    lines, alerts = [], []
    code = _run(["curl", "-s", "-o", "/dev/null", "-w", "%{http_code}", "-m", "8", SITE_URL])
    lines.append(f"  site       {SITE_URL} → {code or 'unreachable'}")
    for part in (gate_section(), review_section(time.time())):
        lines += part[0]
        alerts += part[1]
    lines.append(brain_release())
    ci_state, failed = ci_bot_state()
    if failed:
        lines.append("  batch PR   ? (the bot-state branch could not be read)")
        alerts.append(f"could not read the poller's state from the bot-state branch "
                      f"({failed}); the batch PR was not checked")
        return lines, alerts
    if ci_state is None:
        alerts.append("there is no bot-state branch, so every wikidata-poll run fails at "
                      "restore: seed it with the one-time bootstrap_state dispatch only if it "
                      "never existed, otherwise recreate it (bot/README.md, 'Restore failed')")
    state, source = ((ci_state, "bot-state branch") if ci_state is not None
                     else (batch, "local snapshot; no bot-state branch"))
    if state.get("current_pr"):
        more, a = batch_pr(state["current_pr"], source)
        lines += more
        alerts += a
    return lines, alerts


def snapshot(live: bool = False) -> None:
    batch = json.loads(BOT_STATE.read_text()) if BOT_STATE.exists() else {}
    if not DIGEST.exists():
        print("no digest yet — run `python3 manage/refresh.py` first")
        if live:
            lines, alerts = live_section(batch)
            print("\n".join(lines))
            print("\n  decisions waiting:" if alerts else "\n  nothing waiting (live checks).")
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

    # @[wikidata] batch (offline from bot state; the live gate is under --live)
    if batch:
        print(f"  pipeline   batch {batch.get('batch_num')} · PR #{batch.get('current_pr')}"
              f" · pool {pool['fresh_candidates']} candidates (~{pool['approx_batches']} batches)")

    print()
    print("  work waiting (from the coverage graph):")
    print(f"    formalize     {w['formalize']['total']:>4}  articles with extracted statements awaiting Agent 2")
    print(f"    annotate      {w['annotate']['total']:>4}  central concepts needing a first pass")
    print(f"    coverage map  {w['coverage_gaps']['total']:>4}  moderated articles, low coverage (informational)")
    for r in w["formalize"]["top"][:3]:
        print(f"      · {r['label']} ({r['n_extracted']} stmts, cen {r['centrality_pct']:.0f})")

    alerts = []
    if live:
        lines, alerts = live_section(batch)
        print()
        print("\n".join(lines))

    # Decisions waiting on a human
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
