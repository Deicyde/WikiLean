#!/usr/bin/env python3
"""Aggregate a snapshot of the batch-annotation run and append it to a
time-series log. Designed to be run repeatedly (e.g. every few minutes) so we
build up a record of how the run progresses.

Reads:   cache/.batch_run.log         (one JSON record per finished article)
Appends: cache/.batch_status.jsonl    (one JSON snapshot per invocation)
Prints:  a human-readable summary to stdout.

Usage:
    python batch_status.py            # one snapshot
"""
from __future__ import annotations

import json
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
CACHE = HERE / "cache"
OUT = HERE / "out"
RUN_LOG = CACHE / ".batch_run.log"
TIMESERIES = CACHE / ".batch_status.jsonl"
TOTAL_ARTICLES = 1377  # full WikiProject-Math concept set (pilot + tier2)


def _pct(part: int, whole: int) -> float:
    return round(100 * part / whole, 1) if whole else 0.0


def snapshot() -> dict:
    records: list[dict] = []
    if RUN_LOG.exists():
        for line in RUN_LOG.open():
            line = line.strip()
            if not line:
                continue
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError:
                continue

    # Dedupe by slug (last-wins) so re-runs don't double-count.
    by_slug: dict[str, dict] = {}
    for r in records:
        key = r.get("slug") or r.get("title")
        if key:
            by_slug[key] = r
    recs = list(by_slug.values())

    ok = [r for r in recs if not r.get("error")]
    err = [r for r in recs if r.get("error")]
    errors_by_type = Counter(r["error"].split(":")[0] for r in err)

    # Rendered artifacts actually on disk (ground truth for "done").
    rendered = len(list(OUT.glob("*.html"))) if OUT.exists() else 0

    # The running process may write either key (cost_usd_equiv after the
    # relabel edit, cost_usd before it) — accept both.
    costs = [(r.get("cost_usd_equiv") or r.get("cost_usd") or 0) for r in ok]
    tokens = [r.get("tokens") or 0 for r in ok]
    elapsed = [r.get("elapsed_s") or 0 for r in ok]
    covs = [r["coverage_pct"] for r in ok if r.get("coverage_pct") is not None]

    # Match rate: parse "57/61 matched" -> 57/61.
    match_rates = []
    fully_matched = 0
    for r in ok:
        m = r.get("matched")
        if m and "/" in m:
            a, b = m.split("/")[0], m.split("/")[1].split()[0]
            try:
                a, b = int(a), int(b)
                if b:
                    match_rates.append(a / b)
                    if a == b:
                        fully_matched += 1
            except ValueError:
                pass

    total_cost = sum(costs)
    total_tokens = sum(tokens)
    n_ok = len(ok)
    avg_cost = total_cost / n_ok if n_ok else 0
    avg_tokens = total_tokens / n_ok if n_ok else 0
    remaining = max(0, TOTAL_ARTICLES - rendered)

    # Coverage buckets.
    cov_buckets = {"0-25": 0, "25-50": 0, "50-75": 0, "75-100": 0}
    for c in covs:
        if c < 25: cov_buckets["0-25"] += 1
        elif c < 50: cov_buckets["25-50"] += 1
        elif c < 75: cov_buckets["50-75"] += 1
        else: cov_buckets["75-100"] += 1

    # Avg tool calls per agent.
    a1_tools = [(_r.get("agent1_meta") or {}).get("n_tool_calls", 0) for _r in ok]
    a2_tools = [(_r.get("agent2_meta") or {}).get("n_tool_calls", 0) for _r in ok]

    snap = {
        "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "rendered_on_disk": rendered,
        "total_target": TOTAL_ARTICLES,
        "progress_pct": _pct(rendered, TOTAL_ARTICLES),
        "remaining": remaining,
        "log_records": len(recs),
        "succeeded": n_ok,
        "errored": len(err),
        "errors_by_type": dict(errors_by_type),
        "cost_equiv_total": round(total_cost, 2),
        "cost_equiv_avg": round(avg_cost, 3),
        "cost_equiv_projected_remaining": round(avg_cost * remaining, 2),
        "tokens_total": total_tokens,
        "tokens_avg": int(avg_tokens),
        "tokens_projected_remaining": int(avg_tokens * remaining),
        "coverage_avg": round(sum(covs) / len(covs), 1) if covs else None,
        "coverage_min": min(covs) if covs else None,
        "coverage_max": max(covs) if covs else None,
        "coverage_buckets": cov_buckets,
        "fully_matched": fully_matched,
        "match_rate_avg": round(sum(match_rates) / len(match_rates), 3) if match_rates else None,
        "elapsed_avg_s": round(sum(elapsed) / len(elapsed), 1) if elapsed else None,
        "agent1_tool_calls_avg": round(sum(a1_tools) / len(a1_tools), 1) if a1_tools else None,
        "agent2_tool_calls_avg": round(sum(a2_tools) / len(a2_tools), 1) if a2_tools else None,
    }
    return snap


def main() -> int:
    snap = snapshot()
    TIMESERIES.parent.mkdir(parents=True, exist_ok=True)
    with TIMESERIES.open("a", encoding="utf-8") as f:
        f.write(json.dumps(snap, ensure_ascii=False) + "\n")

    print(f"[{snap['ts']}] progress {snap['rendered_on_disk']}/{snap['total_target']} "
          f"({snap['progress_pct']}%)  ok={snap['succeeded']} err={snap['errored']}")
    print(f"  cost ~${snap['cost_equiv_total']} equiv "
          f"(avg ${snap['cost_equiv_avg']}/art, ~${snap['cost_equiv_projected_remaining']} "
          f"projected for {snap['remaining']} remaining)")
    print(f"  tokens {snap['tokens_total']/1e6:.2f}M (avg {snap['tokens_avg']:,}/art)")
    print(f"  coverage avg={snap['coverage_avg']}% "
          f"[min={snap['coverage_min']} max={snap['coverage_max']}] "
          f"buckets={snap['coverage_buckets']}")
    print(f"  fully-matched={snap['fully_matched']}  "
          f"match-rate-avg={snap['match_rate_avg']}  "
          f"elapsed-avg={snap['elapsed_avg_s']}s")
    print(f"  tool-calls avg: agent1={snap['agent1_tool_calls_avg']} "
          f"agent2={snap['agent2_tool_calls_avg']}")
    if snap["errors_by_type"]:
        print(f"  errors: {snap['errors_by_type']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
