#!/usr/bin/env python3
"""SorryDB campaign prep: freeze the task subset + fetch agent-phase context.

Scope rationale (recorded 2026-07-25, before any run):
- Disk allows ONE repo build at a time (9.5GB free); agent phase needs no
  builds (goal state ships in the row; file context fetched from GitHub raw
  at the pinned commit). So we SAMPLE REPOS, not tasks: every task of a
  chosen repo is included (verification amortizes per repo).
- Repo choice criteria, in order: (1) toolchain already in elan (v4.24-4.27
  family), (2) expected build weight (prefer light/medium; PNT+ included
  deliberately as the mathematically-substantive heavy), (3) domain
  diversity, (4) one pedagogical repo as a calibration floor.
- FROZEN SUBSET (8 repos, ~160 tasks):
    AlexKontorovich/PrimeNumberTheoremAnd  (analysis/NT, heavy, v4.26.0)
    FormalizedFormalLogic/Foundation       (logic, light-medium)
    rkirov/category-theory-in-context-lean (category theory)
    FredRaj3/SemicircleLaw                 (probability/RMT)
    RemyDegenne/brownian-motion            (probability)
    Paul-Lez/PersistentDecomp              (applied topology)
    fpvandoorn/LeanCourse25                (teaching, Mathlib-recent)
    PatrickMassot/GlimpseOfLean            (pedagogical calibration floor)

Fetches, per task: the sorry's file at the pinned commit (raw.githubusercontent),
a +/-60-line window around the sorry, and writes
bench/v2/data/sorrydb/tasks_frozen.jsonl with everything the agent phase needs.
"""
from __future__ import annotations

import json
import sys
import time
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
SDB = HERE / "data" / "sorrydb"
REPOS = [
    # originals whose pins remain fetchable (2026-07-25 probe)
    "AlexKontorovich/PrimeNumberTheoremAnd",
    "FormalizedFormalLogic/Foundation",       # 1 live pin of 21
    "rkirov/category-theory-in-context-lean",
    "FredRaj3/SemicircleLaw",                 # 7 live pins of 21
    "RemyDegenne/brownian-motion",
    "Paul-Lez/PersistentDecomp",
    # replacements: GlimpseOfLean/LeanCourse25/most-of-Foundation/most-of-
    # SemicircleLaw pins were GARBAGE-COLLECTED upstream (74/164 tasks dead —
    # live-repo snapshot rot, recorded for the report). Chosen by the same
    # criteria, pins verified live:
    "Beneficial-AI-Foundation/curve25519-dalek-lean-verify",
    "VCA-EPFL/graphiti",
    "YaelDillies/LeanAPAP",
    "YaelDillies/MiscYD",
]
# Dangling-but-git-fetchable pins verified individually (git fetch by sha):
KNOWN_FETCHABLE = {"ddcb6542f2b62883c8642d3db4ecc115afcce33e"}
WINDOW = 60


def live_commits(repo: str) -> set[str]:
    import subprocess
    try:
        out = subprocess.run(["git", "ls-remote", f"https://github.com/{repo}"],
                             capture_output=True, text=True, timeout=60).stdout
        return {l.split("\t")[0] for l in out.splitlines() if l.strip()}
    except Exception:  # noqa: BLE001
        return set()


def fetch_raw(repo: str, commit: str, path: str, retries: int = 3) -> str | None:
    # curl, not urllib: the framework Python lacks a CA bundle (every https
    # urlopen dies with SSLError — this silently zeroed the first fetch run).
    import subprocess
    url = f"https://raw.githubusercontent.com/{repo}/{commit}/{path}"
    for a in range(retries):
        r = subprocess.run(["curl", "-sfL", "--max-time", "45", url],
                           capture_output=True, text=True)
        if r.returncode == 0 and r.stdout:
            return r.stdout
        time.sleep(3 * (a + 1))
    return None


def main() -> int:
    d = json.load(open(SDB / "SorryDB_2601_1000_evaluation_split.json"))
    rows = d if isinstance(d, list) else d.get("sorries") or []
    tips: dict[str, set[str]] = {}
    chosen = []
    dropped = 0
    for r in rows:
        repo = r["repo"]["remote"].removeprefix("https://github.com/")
        if repo not in REPOS:
            continue
        if repo not in tips:
            tips[repo] = live_commits(repo)
        if r["repo"]["commit"] in tips[repo] or r["repo"]["commit"] in KNOWN_FETCHABLE:
            chosen.append(r)
        else:
            dropped += 1
    print(f"frozen subset: {len(chosen)} live-pin tasks across {len(REPOS)} repos "
          f"({dropped} dead-pin tasks excluded)")
    out = (SDB / "tasks_frozen.jsonl").open("w")
    cache_hits = fails = 0
    file_cache: dict[tuple, str | None] = {}
    for i, r in enumerate(chosen):
        repo = r["repo"]["remote"].removeprefix("https://github.com/")
        commit, path = r["repo"]["commit"], r["location"]["path"]
        key = (repo, commit, path)
        if key not in file_cache:
            file_cache[key] = fetch_raw(repo, commit, path)
        else:
            cache_hits += 1
        text = file_cache[key]
        if text is None:
            fails += 1
            continue
        lines = text.splitlines()
        s, e = r["location"]["start_line"], r["location"]["end_line"]
        lo, hi = max(0, s - 1 - WINDOW), min(len(lines), e + WINDOW)
        ctx = "\n".join(f"{n + 1}| {lines[n]}" for n in range(lo, hi))
        out.write(json.dumps({
            "id": r["id"], "repo": repo, "commit": commit,
            "branch": r["repo"]["branch"], "lean_version": r["repo"]["lean_version"],
            "path": path, "start_line": s, "start_column": r["location"]["start_column"],
            "end_line": e, "end_column": r["location"]["end_column"],
            "goal": (r.get("debug_info") or {}).get("goal"),
            "context_window": ctx, "n_file_lines": len(lines),
        }) + "\n")
        if (i + 1) % 25 == 0:
            print(f"  {i + 1}/{len(chosen)} fetched...", file=sys.stderr)
    out.close()
    print(f"done: {len(chosen) - fails} written, {fails} fetch failures, "
          f"{cache_hits} file-cache hits -> tasks_frozen.jsonl")
    return 0


if __name__ == "__main__":
    sys.exit(main())
