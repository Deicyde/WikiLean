#!/usr/bin/env python3
"""SorryDB verification: kernel-grade every candidate proof, one repo at a time.

Protocol (matches SorryDB's contract — the repo must BUILD with the proof):
  per repo in the frozen subset:
    1. shallow-clone at the pinned commit into --work-dir
    2. `lake exe cache get` when Mathlib is a dependency, then `lake build`
       (the pristine baseline build; its failure disqualifies the repo's
       tasks as 'env_broken', not the agents)
    3. for every agent row of that repo (all arms):
         splice the proof over the sorry span (sanity: the span must
         literally read `sorry`), `lake build <module>` , success iff
         exit 0 AND no 'declaration uses sorry' / new-axiom warning for the
         file; restore the file; append verdict to verify.jsonl
    4. rm -rf the work dir before the next repo (disk: one build at a time)

Durable + resumable: verdicts append to bench/v2/runs/sorrydb/verify.jsonl
keyed by (id, arm, model); existing keys are skipped. Every build's output
tail is preserved in the verdict row (report evidence — nothing gets lost).

Usage:
  python3 bench/v2/verify_sorrydb.py [--repos NAME ...] [--work-dir /tmp/sdbwork]
"""
from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
TASKS = HERE / "data" / "sorrydb" / "tasks_frozen.jsonl"
RUNS = HERE / "runs" / "sorrydb"
VERDICTS = RUNS / "verify.jsonl"
BASELINE_TIMEOUT = 5400
CANDIDATE_TIMEOUT = 900


def sh(cmd: list[str], cwd: Path | None = None, timeout: int = 600) -> tuple[int, str]:
    try:
        r = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True,
                           timeout=timeout)
        return r.returncode, (r.stdout + r.stderr)[-4000:]
    except subprocess.TimeoutExpired:
        return 124, f"timeout after {timeout}s"


def clone_at(repo: str, commit: str, dest: Path) -> tuple[bool, str]:
    dest.mkdir(parents=True, exist_ok=True)
    for cmd in (["git", "init", "-q"],
                ["git", "remote", "add", "origin", f"https://github.com/{repo}"],
                ["git", "fetch", "-q", "--depth", "1", "origin", commit],
                ["git", "checkout", "-q", "FETCH_HEAD"]):
        rc, out = sh(cmd, cwd=dest, timeout=900)
        if rc != 0:
            return False, f"{' '.join(cmd)}: {out}"
    return True, "ok"


def module_of(path: str) -> str:
    return path.removesuffix(".lean").replace("/", ".")


def splice(file: Path, t: dict, proof: str) -> str | None:
    """Replace the sorry span with the proof. Returns an error string or None.
    Columns in SorryDB rows are 0-indexed codepoints (their span exactly
    covers the literal `sorry`, which we assert)."""
    lines = file.read_text().splitlines(keepends=True)
    sl, sc = t["start_line"] - 1, t["start_column"]
    el, ec = t["end_line"] - 1, t["end_column"]
    try:
        span = (lines[sl][sc:ec] if sl == el else
                lines[sl][sc:] + "".join(lines[sl + 1:el]) + lines[el][:ec])
    except IndexError:
        return "span out of range"
    if span.strip() != "sorry":
        return f"span is {span.strip()[:30]!r}, not 'sorry'"
    indent = " " * t["start_column"]
    body = proof.replace("\n", "\n" + indent) if "\n" in proof else proof
    lines[sl:el + 1] = [lines[sl][:sc] + body + lines[el][ec:]]
    file.write_text("".join(lines))
    return None


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--repos", nargs="*", default=None,
                    help="short names to verify (default: all with agent rows)")
    ap.add_argument("--work-dir", type=Path, default=Path("/tmp/sdbwork"))
    args = ap.parse_args()

    tasks = {t["id"]: t for t in
             (json.loads(l) for l in TASKS.read_text().splitlines() if l.strip())}
    rows = []
    for f in RUNS.glob("*/*/*.json"):
        r = json.loads(f.read_text())
        if r.get("proof") and not r.get("gave_up"):
            rows.append(r)
    done = set()
    if VERDICTS.exists():
        for l in VERDICTS.read_text().splitlines():
            if l.strip():
                v = json.loads(l)
                done.add((v["id"], v["arm"], v["model"]))
    rows = [r for r in rows if (r["id"], r["arm"], r["model"]) not in done]
    by_repo: dict[str, list[dict]] = {}
    for r in rows:
        by_repo.setdefault(r["repo"], []).append(r)
    if args.repos:
        by_repo = {k: v for k, v in by_repo.items()
                   if k.split("/")[-1] in set(args.repos)}
    print(f"{sum(map(len, by_repo.values()))} candidates across "
          f"{len(by_repo)} repos (skipped {len(done)} already-verified)")

    vout = VERDICTS.open("a")
    for repo, cands in by_repo.items():
        commit = cands[0]["commit"]
        work = args.work_dir / repo.split("/")[-1]
        print(f"\n=== {repo} @ {commit[:12]} — {len(cands)} candidates ===")
        if work.exists():
            shutil.rmtree(work)
        ok, msg = clone_at(repo, commit, work)
        if not ok:
            print(f"  CLONE FAILED: {msg[:200]}")
            for r in cands:
                vout.write(json.dumps({"id": r["id"], "arm": r["arm"],
                                       "model": r["model"], "ok": False,
                                       "verdict": "env_broken",
                                       "detail": f"clone: {msg[:300]}"}) + "\n")
            vout.flush()
            continue
        # Mathlib olean cache when available, then the pristine baseline build.
        lakefile = (work / "lakefile.lean").exists() or (work / "lakefile.toml").exists()
        if lakefile:
            sh(["lake", "exe", "cache", "get"], cwd=work, timeout=1800)
        t0 = time.monotonic()
        rc, out = sh(["lake", "build"], cwd=work, timeout=BASELINE_TIMEOUT)
        print(f"  baseline build: rc={rc} in {time.monotonic() - t0:.0f}s")
        if rc != 0:
            for r in cands:
                vout.write(json.dumps({"id": r["id"], "arm": r["arm"],
                                       "model": r["model"], "ok": False,
                                       "verdict": "env_broken",
                                       "detail": f"baseline: {out[-300:]}"}) + "\n")
            vout.flush()
            shutil.rmtree(work, ignore_errors=True)
            continue
        for r in cands:
            t = tasks[r["id"]]
            file = work / t["path"]
            orig = file.read_text()
            err = splice(file, t, r["proof"])
            if err:
                verdict = {"ok": False, "verdict": "unspliceable", "detail": err}
            else:
                t1 = time.monotonic()
                rc, out = sh(["lake", "build", module_of(t["path"])],
                             cwd=work, timeout=CANDIDATE_TIMEOUT)
                bad = ("uses 'sorry'" in out or "uses sorry" in out
                       or "axiom" in out.lower().split("warning:")[-1][:200])
                okv = rc == 0 and not bad
                verdict = {"ok": okv,
                           "verdict": "proved" if okv else
                           ("timeout" if rc == 124 else "failed"),
                           "build_s": round(time.monotonic() - t1, 1),
                           "detail": out[-500:]}
            file.write_text(orig)
            vout.write(json.dumps({"id": r["id"], "arm": r["arm"],
                                   "model": r["model"], "repo": repo,
                                   **verdict}) + "\n")
            vout.flush()
            print(f"    {r['arm']:<3} {r['id'][:16]} -> {verdict['verdict']}")
        shutil.rmtree(work, ignore_errors=True)
    vout.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
