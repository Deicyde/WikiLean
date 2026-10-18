#!/usr/bin/env python3
"""Summarize the nightly host's review errors and the stage-0 drift trigger.

Run on the host that runs org.wikilean.moderate, from the checkout root. It only
reads the gitignored run logs under site/cache/ (no network, no writes):

  .moderate_run.log        one JSON rec per review/new article, with the error text
  .decisions.jsonl         one line per article per pass: ts, run_id, mode, outcome
  .wp_update_report.jsonl  one line per stage-0 article whose anchors failed

Two reports:

  1. The last N review runs: articles, errors, tokens, the distinct error strings
     with counts and example slugs, the fastest failing article's elapsed time,
     and which slugs failed in more than one run (the review queue does not
     advance past a failed article). Each distinct error gets a likely cause
     from docs/OPERATIONS.md.
  2. The ROADMAP P3 trigger for re-anchoring stages 1-2 ("stage-0 clears <90%"),
     over the last D days: re-pinned / (re-pinned + anchors failed), per attempt
     and per distinct slug, plus the slugs that block the wp-update queue head,
     and a warning once one wp-update run nears /api/work's 1000-row cap.

Stdlib only and Python 3.9 compatible, so an older checkout can run the current
version without updating:

  git fetch origin && git show origin/main:manage/nightly_health.py | python3 - --cache site/cache
  python3 manage/nightly_health.py [--cache DIR] [--runs N] [--days D]
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
from collections import Counter, OrderedDict
from datetime import datetime, timezone
from pathlib import Path

P3_THRESHOLD = 0.90  # docs/ROADMAP.md: re-anchoring stages 1-2 trigger at "<90%"
P3_MIN_SLUGS = 20    # fewer distinct drifted slugs than this is too few to judge
WP_UPDATE_CAP = 1000       # /api/work's wp-update row cap (wiki/src/index.ts)
WP_UPDATE_WARN_ROWS = 800  # warn when one wp-update run takes this many articles
OLD_WP_UPDATE_PAGE = 100  # what /api/work served wp-update before its cap was raised

# Ordered: the first matching pattern names the likely cause, matched against
# the raw (untruncated) error. The names are the "Likely cause" column of the
# "What each outcome means" table in docs/OPERATIONS.md.
CAUSES = [
    ("python3 on the job's PATH",
     r"^extract_failed|'python3'|xcrun|xcode-select|commandlinetools"),
    ("Mathlib checkout path", r"working directory does not exist"),
    ("Full Disk Access", r"operation not permitted|exit (?:status |code )?126\b"),
    ("API-key credential", r"credit balance"),
    ("Claude login",
     r"not logged in|/login|invalid api key|oauth|authentication|unauthori[sz]ed"
     r"|\b401\b|keychain"),
    ("plan or usage limit",
     r"hit your|usage limit|weekly limit|limit reached|resets \d|quota"
     r"|overloaded|\b529\b|rate.?limit(?!ed_429)"),
    ("model id", r"selected model|\bmodel\b.*(not.?found|not available|access)"
     r"|not_found_error"),
    ("Worker write", r"^(network_post_failed|network_put_failed|rate_limited_429"
     r"|post_\d|put_\d|human_lost_422|create_422)"),
    ("claude CLI startup timeout", r"control request timeout: initialize"),
    ("API stream timeout", r"stream idle timeout"),
    ("claude CLI",
     r"claude code not found|command failed with exit code|cliconnection"
     r"|processerror|error result|jsondecode|exec format|permission denied"),
    ("Wikipedia fetch", r"^fetch_failed"),
    ("Worker read", r"^get_failed_"),
    ("agent output", r"^(agent[12]_no_json|final_read_failed|render_failed)"),
]
# extract_failed and fetch_failed name one article's failure. They point at the
# host (python3, Wikipedia) only when every article in the run shows them;
# site/moderate.py's ARTICLE_SPECIFIC_ERROR_RE treats both as article-specific.
HOST_WIDE_ONLY_ON_EVERY_ARTICLE = re.compile(r"^(extract_failed|fetch_failed)\b")


def _stage(error):
    """'extract_failed' or 'fetch_failed' if the error names that stage, else None."""
    m = HOST_WIDE_ONLY_ON_EVERY_ARTICLE.match(str(error).strip())
    return m.group(1) if m else None

_HOME = re.compile(r"/(?:Users|home)/[^/\s'\"]+")
_SECRET = re.compile(
    r"sk-ant-[\w-]+|gh[pousr]_[A-Za-z0-9]{20,}|github_pat_\w+|Bearer\s+\S+"
    r"|\b(?=[A-Za-z]*\d)[A-Za-z0-9]{32,}\b")
_EMAIL = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)*\.[A-Za-z]{2,}\b")


def clean(text, limit: int = 200) -> str:
    """One display line: no home directories, emails or token-like strings."""
    s = " ".join(str(text).split())
    s = _HOME.sub("~", s)
    s = _SECRET.sub("[redacted]", s)
    s = _EMAIL.sub("[email]", s)
    return s if len(s) <= limit else s[: limit - 1] + "…"


def likely_cause(error) -> str:
    low = " ".join(str(error).split()).lower()
    for name, pattern in CAUSES:
        if re.search(pattern, low):
            return name
    return "unclassified"


def read_jsonl(path: Path) -> list:
    out = []
    try:
        with path.open(encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue
                if isinstance(rec, dict):
                    out.append(rec)
    except FileNotFoundError:
        pass
    return out


def _utc(ms) -> str:
    if not isinstance(ms, (int, float)):
        return "?"
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%MZ")


def review_runs(run_log: list, decisions: list, n_runs: int) -> list:
    """The last n_runs review runs, oldest first, from the per-article recs.

    The run log has no timestamps, so 'first result' is the run's earliest
    decisions line (each is written when an article finishes). A rec's mode is
    its 'op'; recs without one (job_crashed) take the mode of the decisions
    line for the same run and slug. `moderate.py all` runs therefore count
    their review articles too. Dry runs (a 'dry-run' decisions line, or a rec
    marked dry_run) are left out: they write nothing and say nothing about the
    nightly."""
    if n_runs <= 0:
        return []
    dry = ({d.get("run_id") for d in decisions if d.get("outcome") == "dry-run"}
           | {r.get("run_id") for r in run_log if r.get("dry_run")})
    mode_of, start_of = {}, {}
    for d in decisions:
        rid = d.get("run_id")
        if not rid:
            continue
        mode_of.setdefault((rid, d.get("slug")), d.get("mode"))
        ts = d.get("ts")
        if isinstance(ts, (int, float)) and (rid not in start_of or ts < start_of[rid]):
            start_of[rid] = ts
    by_run = OrderedDict()
    for rec in run_log:
        rid = rec.get("run_id")
        if not rid or rid in dry:
            continue
        mode = rec.get("op") or mode_of.get((rid, rec.get("slug")))
        if mode != "review":
            continue
        by_run.setdefault(rid, []).append(rec)
    runs = []
    for rid, recs in list(by_run.items())[-n_runs:]:
        errs = [r for r in recs if r.get("error")]
        # A stage's failure is host-wide only if every article that reached the
        # stage failed there: the fetch runs after the Worker read, the extract
        # after the fetch.
        stage_failed = Counter(_stage(r["error"]) for r in errs)
        read_failed = sum(1 for r in errs if str(r["error"]).strip().startswith("get_failed_"))
        reached = {"fetch_failed": len(recs) - read_failed,
                   "extract_failed": len(recs) - read_failed - stage_failed["fetch_failed"]}
        groups = OrderedDict()
        for r in errs:
            key = clean(r["error"])
            g = groups.setdefault(key, {"error": key, "count": 0, "slugs": [],
                                        "elapsed": [], "raw": r["error"]})
            g["count"] += 1
            if len(g["slugs"]) < 3:
                g["slugs"].append(str(r.get("slug", "?")))
            if isinstance(r.get("elapsed_s"), (int, float)):
                g["elapsed"].append(float(r["elapsed_s"]))
        for g in groups.values():
            raw = g.pop("raw")
            g["cause"] = likely_cause(raw)
            stage = _stage(raw)
            if stage and stage_failed[stage] < reached[stage]:
                g["cause"] = "article-specific"
            # Older checkouts start elapsed_s before annotate_one waits for a
            # concurrency slot, so queued articles also count that wait; the
            # fastest one is the closest to a single article's own time.
            g["min_elapsed_s"] = (round(min(g["elapsed"]), 1)
                                  if g["elapsed"] else None)
            del g["elapsed"]
        runs.append({
            "run_id": rid,
            "started": _utc(start_of.get(rid)),
            "articles": len(recs),
            "errors": len(errs),
            "tokens": sum(r.get("tokens") or 0 for r in recs),
            "error_groups": sorted(groups.values(), key=lambda g: -g["count"]),
            "failed_slugs": sorted({str(r.get("slug", "?")) for r in errs}),
        })
    return runs


def repeated_failures(runs: list) -> list:
    """(slug, number of runs it failed in) for slugs that failed in 2+ runs."""
    c = Counter(s for run in runs for s in run["failed_slugs"])
    return [(s, n) for s, n in c.most_common() if n > 1]


def drift_trigger(decisions: list, report: list, days: int, now: float) -> dict:
    """Stage-0 clearance over the window, from the wp-update decisions lines.

    posted = re-pinned. A noop with anchors matched < total = anchors failed
    (the article needs stage 1/2). Everything else (already current, no
    latest_revid, 409 races, fetch errors) is not a drift attempt. Dry runs
    are dropped: their needs-work lines look like real ones in decisions, so a
    run counts as dry if any of its lines is 'dry-run' or its report rows say
    dry_run."""
    cutoff = (now - days * 86400) * 1000
    dry = ({d.get("run_id") for d in decisions if d.get("outcome") == "dry-run"}
           | {r.get("run_id") for r in report if r.get("dry_run")})
    repinned, failed, errors = [], [], 0
    per_run = Counter()
    for d in decisions:
        if d.get("mode") != "wp-update" or d.get("run_id") in dry:
            continue
        ts = d.get("ts")
        if not isinstance(ts, (int, float)) or ts < cutoff:
            continue
        per_run[d.get("run_id")] += 1
        slug, outcome, anchors = d.get("slug"), d.get("outcome"), d.get("anchors")
        if outcome == "posted":
            repinned.append(slug)
        elif outcome == "error":
            errors += 1
        elif (outcome == "noop" and isinstance(anchors, dict)
              and isinstance(anchors.get("matched"), int)
              and isinstance(anchors.get("total"), int)
              and anchors["matched"] < anchors["total"]):
            failed.append(slug)
    attempts = len(repinned) + len(failed)
    rp, fl = set(repinned), set(failed)
    blocked = fl - rp
    distinct = len(rp | fl)
    report_rows = 0
    for r in report:
        if r.get("dry_run") or r.get("run_id") in dry:
            continue
        try:
            ts = datetime.fromisoformat(str(r.get("ts"))).timestamp() * 1000
        except ValueError:
            continue
        if ts >= cutoff:
            report_rows += 1
    head = Counter(s for s in failed if s in blocked).most_common(10)
    return {
        "days": days,
        "repinned": len(repinned),
        "anchors_failed": len(failed),
        "errors": errors,
        "clear_rate": (len(repinned) / attempts) if attempts else None,
        "distinct_slugs": distinct,
        "distinct_cleared": len(rp),
        "distinct_clear_rate": (len(rp) / distinct) if distinct else None,
        "never_cleared": len(blocked),
        "report_rows": report_rows,
        "queue_head": head,
        "largest_run": max(per_run.values(), default=0),
    }


def _pct(x) -> str:
    return "n/a" if x is None else f"{100 * x:.1f}%"


def print_report(runs: list, drift: dict, cache: Path) -> None:
    print(f"WikiLean nightly health: {cache}")
    print()
    if not runs:
        print("review: no review runs found in .moderate_run.log")
    for run in runs:
        print(f"review run {run['run_id']}  first result {run['started']}  "
              f"articles {run['articles']}  errors {run['errors']}  "
              f"tokens {run['tokens']:,}")
        for g in run["error_groups"]:
            t = g["min_elapsed_s"]
            t_s = f"fastest {t}s" if t is not None else "time ?"
            print(f"  {g['count']:>3}x  [{g['cause']}; {t_s}]  {g['error']}")
            print(f"        e.g. {', '.join(g['slugs'])}")
    rep = repeated_failures(runs)
    if rep:
        print()
        print(f"slugs that failed in more than one of these {len(runs)} runs "
              f"(the queue does not advance past a failed review):")
        for slug, n in rep[:20]:
            print(f"  {n} runs  {slug}")
    if runs:
        last = runs[-1]
        print()
        if last["errors"] and last["errors"] == last["articles"]:
            causes = sorted({g["cause"] for g in last["error_groups"]})
            print(f"verdict: the latest run failed on every article; likely cause: "
                  f"{', '.join(causes)} (docs/OPERATIONS.md, 'What each outcome means').")
        elif last["errors"]:
            print(f"verdict: the latest run had {last['errors']} error(s) "
                  f"out of {last['articles']} articles.")
        else:
            print("verdict: the latest review run had no errors.")
    print()
    print(f"drift (wp-update stage-0), last {drift['days']} days:")
    print(f"  attempts      {drift['repinned'] + drift['anchors_failed']} "
          f"(re-pinned {drift['repinned']}, anchors failed {drift['anchors_failed']}; "
          f"{drift['errors']} errors not counted)")
    print(f"  clear rate    {_pct(drift['clear_rate'])} per attempt, "
          f"{_pct(drift['distinct_clear_rate'])} per distinct slug "
          f"({drift['distinct_cleared']}/{drift['distinct_slugs']})")
    print(f"  report rows   {drift['report_rows']} in .wp_update_report.jsonl "
          f"(should equal anchors failed)")
    if drift["largest_run"] >= WP_UPDATE_WARN_ROWS:
        print(f"  queue size    WARNING: a wp-update run took {drift['largest_run']} "
              f"articles; /api/work serves at most {WP_UPDATE_CAP}, and past that "
              f"failed re-pins hold the queue head again (docs/OPERATIONS.md, "
              f"'Drift: measuring the stage-0 trigger')")
    rate = drift["distinct_clear_rate"]
    if rate is None:
        print("  P3 trigger    no stage-0 attempts in the window")
    elif drift["distinct_slugs"] < P3_MIN_SLUGS:
        print(f"  P3 trigger    too few drifted slugs to judge "
              f"({drift['distinct_slugs']} < {P3_MIN_SLUGS})")
    elif rate < P3_THRESHOLD:
        print(f"  P3 trigger    MET: stage-0 clears {_pct(rate)} of drifted articles "
              f"(< {P3_THRESHOLD:.0%}); docs/ROADMAP.md gates re-anchoring stages 1-2 on this")
    else:
        print(f"  P3 trigger    not met ({_pct(rate)} >= {P3_THRESHOLD:.0%})")
    if rate is not None and drift["largest_run"] == OLD_WP_UPDATE_PAGE:
        print(f"  sample        the largest wp-update run in the window took exactly "
              f"{OLD_WP_UPDATE_PAGE} articles, a full old page, so the attempts over-represent "
              f"articles that keep failing; read the clear rate as a lower bound")
    if drift["queue_head"]:
        print(f"  never cleared {drift['never_cleared']} slugs; most-retried:")
        for slug, n in drift["queue_head"]:
            print(f"    {n:>3}x  {slug}")


def main(argv=None) -> int:
    here = Path(sys.argv[0]).resolve().parent if sys.argv and sys.argv[0] not in ("", "-") else None
    default_cache = (here.parent / "site" / "cache"
                     if here is not None and (here.parent / "site" / "cache").is_dir()
                     else Path("site") / "cache")
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--cache", type=Path, default=default_cache,
                    help="the host checkout's site/cache directory")
    ap.add_argument("--runs", type=lambda v: max(1, int(v)), default=5,
                    help="review runs to summarize (at least 1)")
    ap.add_argument("--days", type=int, default=30, help="drift window in days")
    args = ap.parse_args(argv)
    cache = args.cache
    if not cache.is_dir():
        print(f"no such directory: {cache} (run from the checkout root or pass --cache)",
              file=sys.stderr)
        return 2
    decisions = read_jsonl(cache / ".decisions.jsonl")
    runs = review_runs(read_jsonl(cache / ".moderate_run.log"), decisions, args.runs)
    drift = drift_trigger(decisions, read_jsonl(cache / ".wp_update_report.jsonl"),
                          args.days, time.time())
    print_report(runs, drift, cache)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
