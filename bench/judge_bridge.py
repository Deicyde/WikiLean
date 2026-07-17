#!/usr/bin/env python3
"""LLM judge for Bridge Experiment run outputs — dual strict/evaluated grading.

Grades each bench/data/runs/<arm>/<task>.json against the gold formal statement,
following the TheoremGraph protocol (docs/research/BRIDGE-EXPERIMENT.md):
  strict    — the produced statement expresses the SAME proposition as gold
              (same hypotheses, same conclusion, up to renaming/notation)
  evaluated — high-confidence mathematical equivalence even if shaped differently

The judge is NOT the primary metric (BEq+ is, when the rig lands) and is assumed
over-generous until calibrated: TheoremGraph dropped their first judge after an
expert audit found 5/10 over-graded. `--calibration` emits a 50-item sample for
human grading; report judge-human agreement before trusting campaign numbers.

Shells the `claude` CLI on Max auth (ANTHROPIC_API_KEY scrubbed — CLAUDE.md).
Writes verdicts next to each run file: <task>.judge.json. Resumable.

Usage:
  python3 bench/judge_bridge.py --arm D [--model claude-sonnet-5] [--limit N]
  python3 bench/judge_bridge.py --calibration 50   # sample across arms for humans
"""
from __future__ import annotations

import argparse
import json
import os
import random
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
RUNS = HERE / "data" / "runs"
TASKS = HERE / "data" / "bridge_tasks.jsonl"

PROMPT = """You are grading a Lean 4 autoformalization attempt against a gold statement.

INFORMAL STATEMENT (the task):
{informal}

GOLD Lean 4 statement:
```lean
{gold}
```

PRODUCED Lean 4 statement:
```lean
{produced}
```

Grade ONLY the mathematical content of the statement (ignore the proof body; both
end in sorry). Judge:
1. strict: does the produced statement express the SAME proposition as the gold —
   same hypotheses (none added, none dropped, none weakened/strengthened), same
   conclusion — allowing variable renaming, notation, argument order, and
   equivalent spellings of the same structure (e.g. bundled vs unbundled forms of
   the SAME typeclass)? true/false.
2. evaluated: are the two statements mathematically equivalent with high
   confidence, even if shaped differently (e.g. contrapositive, iff split,
   an equivalent standard characterization)? true/false. If you cannot decide
   confidently, answer false — do NOT give benefit of the doubt.
3. defects: list every concrete defect you see in the produced statement
   (missing hypothesis X, wrong quantifier on Y, cites nonexistent decl Z, …);
   empty list if none.

Answer with ONLY a JSON object: {{"strict": bool, "evaluated": bool, "defects": [".."]}}"""


def load_tasks() -> dict[str, dict]:
    rows = {}
    with TASKS.open() as fh:
        for line in fh:
            if not line.strip():
                continue
            row = json.loads(line)
            if "id" in row:
                rows[row["id"]] = row
    return rows


def judge_one(task: dict, run: dict, model: str) -> dict:
    prompt = PROMPT.format(
        informal=task["informal_statement"][:4000],
        gold=task["gold_formal"][:4000],
        produced=(run.get("output_lean") or "(no output)")[:4000],
    )
    env = {k: v for k, v in os.environ.items()
           if k not in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL", "USE_STAGING_OAUTH", "USE_LOCAL_OAUTH", "CLAUDE_CODE_OAUTH_SCOPES")}
    t0 = time.monotonic()
    proc = subprocess.run(
        ["claude", "-p", "--model", model, "--max-turns", "1"],
        input=prompt, capture_output=True, text=True, timeout=180, env=env,
    )
    out = proc.stdout.strip()
    # the CLI may wrap the JSON in prose/fences; take the outermost braces
    try:
        verdict = json.loads(out[out.index("{"): out.rindex("}") + 1])
    except Exception:
        verdict = {"strict": False, "evaluated": False,
                   "defects": [f"judge-unparseable: {out[:200]}"]}
    verdict["judge_model"] = model
    verdict["judge_wall_s"] = round(time.monotonic() - t0, 1)
    return verdict


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--arm", help="grade one arm's runs (A/B/C/D/E)")
    ap.add_argument("--model", default="claude-sonnet-5")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--calibration", type=int, default=0,
                    help="emit N graded items (stratified across arms) for human audit")
    args = ap.parse_args()

    tasks = load_tasks()

    if args.calibration:
        judged = sorted(RUNS.glob("*/*.judge.json"))
        random.seed(20260716)
        pick = random.sample(judged, min(args.calibration, len(judged)))
        out = HERE / "data" / "judge_calibration.jsonl"
        with out.open("w") as fh:
            for p in pick:
                run = json.loads(p.with_suffix("").with_suffix(".json").read_text())
                task = tasks.get(run["task_id"], {})
                fh.write(json.dumps({
                    "run": str(p), "informal": task.get("informal_statement"),
                    "gold": task.get("gold_formal"), "produced": run.get("output_lean"),
                    "judge": json.loads(p.read_text()),
                    "human_strict": None, "human_evaluated": None,   # fill by hand
                }) + "\n")
        print(f"wrote {len(pick)} calibration items -> {out} "
              f"(fill human_strict/human_evaluated, then report agreement)")
        return 0

    if not args.arm:
        ap.error("--arm required (or --calibration)")
    arm_dir = RUNS / args.arm
    run_files = sorted(p for p in arm_dir.glob("*.json") if ".judge" not in p.name)
    if args.limit:
        run_files = run_files[: args.limit]
    done = graded = 0
    for p in run_files:
        out = p.with_name(p.stem + ".judge.json")
        if out.exists():
            done += 1
            continue
        run = json.loads(p.read_text())
        task = tasks.get(run["task_id"])
        if not task:
            print(f"  ! no task row for {run['task_id']} — skipping", file=sys.stderr)
            continue
        verdict = judge_one(task, run, args.model)
        out.write_text(json.dumps(verdict, indent=1))
        graded += 1
        tick = "S" if verdict.get("strict") else ("e" if verdict.get("evaluated") else ".")
        print(f"  [{graded}] {run['task_id']} {tick}")
    print(f"{args.arm}: graded {graded} new, {done} already done, "
          f"{len(run_files)} run files total")
    return 0


if __name__ == "__main__":
    sys.exit(main())
