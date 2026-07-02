#!/usr/bin/env python3
"""Ground-truth snapshot — the session-start read of the control plane.

Prints where WikiLean actually is right now, so a session opens from truth
instead of a stale handoff: coverage, the Agent-2 backlog, worklist depths, tag
pool runway, graph freshness, the live @[wikidata] batch, and uncommitted git
state — then the decisions that are waiting on a human.

Offline and instant by default (reads manage/data/digest.json + local state).
``--live`` adds two network checks: the site returns 200, and the bot gate says
act|wait. ``--refresh`` regenerates the digest first (see refresh.py).

    python3 manage/status.py
    python3 manage/status.py --live
    python3 manage/status.py --refresh --live
"""
from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
HERE = Path(__file__).resolve().parent
DATA = HERE / "data"
DIGEST = DATA / "digest.json"
BOT_STATE = ROOT / "bot" / "state" / "bot_state.json"
SITE_URL = "https://wikilean.jackmccarthy.org"

# Rule-of-thumb thresholds (see HANDOFF §4).
LOW_POOL_BATCHES = 3.0     # generate more tags below this
STALE_DIGEST_HOURS = 36    # nightly refresh should keep it under a day


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


def snapshot(live: bool = False) -> None:
    if not DIGEST.exists():
        print("no digest yet — run `python3 manage/refresh.py` first")
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
    batch = json.loads(BOT_STATE.read_text()) if BOT_STATE.exists() else {}
    gate = _run([sys.executable, "bot/poll.py", "--mathlib", "/tmp/unused", "--decide"]) if live else None
    gate_s = f"  gate: {gate}" if gate else ""
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
    if live:
        code = _run(["curl", "-s", "-o", "/dev/null", "-w", "%{http_code}", "-m", "8", SITE_URL])
        print(f"\n  site       {SITE_URL} → {code or 'unreachable'}")

    # Decisions waiting on a human
    alerts = []
    if fresh.get("graph_older_than_annotations"):
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
        alerts.append("digest is stale — `python3 manage/refresh.py --pull`")

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
