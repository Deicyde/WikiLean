#!/usr/bin/env python3
"""Factorial Stage-2 driver: blind LLM-judge equivalence grading of the 400
2x2-factorial outputs (arms Ep/X/J/Dp x 100 fresh tasks).

Protocol IDENTICAL to bench/analysis/judge_fresh_run.py (the v3 fresh-set judge
pass), per the preregistration docs/research/BRIDGE-FACTORIAL.md section 5
secondary endpoint 2: prompt template imported VERBATIM from
judge_bridge.PROMPT ({informal, gold, produced} only), gold shown with
gold_context, arm-substring blinding scan aborts on leak, no-tools
claude-sonnet-5 judge from an empty scratch cwd, --strict-mcp-config,
--max-turns 1, concurrency 3, 429 wait-retry, env scrubbed (Max auth).

Differences from judge_fresh_run.py, all mechanical re-targeting:
  * ARMS = Ep/X/J/Dp; rows read from bench/data/runs_factorial/;
  * verdicts -> bench/analysis/judge_factorial/<arm>/<task>.judge.json;
  * the forbidden-substring list additionally covers the factorial arm labels
    and run directory (the joined/unjoined tool names were already covered);
  * --consistency re-grades a fixed seed-stratified 40-item subset
    (10 per arm, seed 20260727 — the same protocol seed) into
    judge_factorial/consistency/.

Run:
  python3 bench/analysis/judge_factorial_run.py --blinding-check-only
  python3 bench/analysis/judge_factorial_run.py            # main pass (400)
  python3 bench/analysis/judge_factorial_run.py --consistency   # 40 re-grades
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import random
import re
import subprocess
import sys
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

HERE = Path(__file__).resolve().parent          # bench/analysis
BENCH = HERE.parent                              # bench/
sys.path.insert(0, str(BENCH))
import judge_bridge  # noqa: E402  — PROMPT reused verbatim
from construct import _ctx_text  # noqa: E402

RUNS = BENCH / "data" / "runs_factorial"
FRESH_TASKS = BENCH / "data" / "fresh_tasks.jsonl"
OUT_ROOT = HERE / "judge_factorial"
CONSISTENCY_ROOT = OUT_ROOT / "consistency"
ARMS = ["Ep", "X", "J", "Dp"]
JUDGE_MODEL = "claude-sonnet-5"
CONCURRENCY = 3
RETRY_WAIT_S = 600
MAX_ATTEMPTS = 3
CONSISTENCY_SEED = 20260727
CONSISTENCY_PER_ARM = 10

# Substrings that would reveal arm identity / harness internals to the judge.
# Superset of judge_fresh_run._FORBIDDEN plus the factorial arm labels/paths.
_FORBIDDEN = [
    "mcp__", "loogle", "decl_grep", "decl_read", "brain_transfer", "brain_cell",
    "brain_search", "brain_bridge", "wiki_get", "wiki_search", "wikibrain",
    "tool_calls", "transcript", "bench/data", "runs/A", "runs/B", "runs/C",
    "runs/D", "runs/E", "arm A", "arm B", "arm C", "arm D", "arm E",
    "no_tools", "wikilean",
    "runs_factorial", "arm Ep", "arm Dp", "arm J", "arm X", "factorial",
    "decl_exists", "brain_neighborhood", "brain_snippets", "brain_filter",
    "verifier arm", "join arm",
]

_print_lock = threading.Lock()


def load_fresh_tasks() -> dict[str, dict]:
    rows: dict[str, dict] = {}
    for line in FRESH_TASKS.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        r = json.loads(line)
        if "id" in r:
            rows[r["id"]] = r
    assert len(rows) == 100, len(rows)
    return rows


def gold_for_judge(task: dict) -> str:
    """gold_context (variable/open binders) + gold_formal. Arm-independent."""
    ctx = _ctx_text(task.get("gold_context"))
    return (ctx + "\n\n" + task["gold_formal"]).strip() if ctx else task["gold_formal"]


def build_prompt(task: dict, run: dict) -> str:
    return judge_bridge.PROMPT.format(
        informal=task["informal_statement"][:4000],
        gold=gold_for_judge(task)[:4000],
        produced=(run.get("output_lean") or "(no output)")[:4000],
    )


def blinding_scan(prompts: dict[tuple[str, str], str]) -> list[str]:
    bad = []
    for (arm, tid), p in prompts.items():
        low = p.lower()
        for t in _FORBIDDEN:
            if t.lower() in low:
                bad.append(f"{arm}/{tid}: contains {t!r}")
    return bad


_SCRATCH = Path(tempfile.mkdtemp(prefix="judge-factorial-cwd-"))  # empty, outside repo


def scrubbed_env() -> dict:
    env = dict(os.environ)
    for k in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL",
              "USE_STAGING_OAUTH", "USE_LOCAL_OAUTH", "CLAUDE_CODE_OAUTH_SCOPES"):
        env.pop(k, None)
    return env


def judge_once(prompt: str) -> dict:
    """One CLI call. Returns {'ok':bool,'verdict':..,'raw':..,'cost':..,'retryable':bool}."""
    t0 = time.monotonic()
    try:
        proc = subprocess.run(
            ["claude", "-p", "--model", JUDGE_MODEL, "--max-turns", "1",
             "--strict-mcp-config",
             "--disallowedTools",
             "Bash,Read,Write,Edit,Glob,Grep,WebFetch,WebSearch,Task,TodoWrite,NotebookEdit",
             "--output-format", "json"],
            input=prompt, capture_output=True, text=True, timeout=300,
            env=scrubbed_env(), cwd=_SCRATCH,
        )
    except subprocess.TimeoutExpired:
        return {"ok": False, "retryable": True, "err": "cli timeout 300s",
                "wall_s": round(time.monotonic() - t0, 1), "cost": 0.0}
    wall = round(time.monotonic() - t0, 1)
    out = proc.stdout.strip()
    envelope: dict = {}
    try:
        envelope = json.loads(out)
    except Exception:
        pass
    text = envelope.get("result") if isinstance(envelope, dict) else None
    if text is None:
        text = out
    cost = float(envelope.get("total_cost_usd") or 0.0) if isinstance(envelope, dict) else 0.0
    err_blob = f"{text} {proc.stderr[:500]}"
    is_err = bool(isinstance(envelope, dict) and envelope.get("is_error"))
    retryable = bool(re.search(r"429|rate.?limit|session limit|overloaded|usage limit",
                               err_blob, re.I))
    if is_err or not text:
        return {"ok": False, "retryable": retryable,
                "err": (text or proc.stderr or "empty response")[:300],
                "wall_s": wall, "cost": cost}
    try:
        verdict = json.loads(text[text.index("{"): text.rindex("}") + 1])
        assert isinstance(verdict.get("strict"), bool)
        assert isinstance(verdict.get("evaluated"), bool)
    except Exception:
        return {"ok": False, "retryable": retryable,
                "err": f"unparseable: {text[:300]}", "wall_s": wall, "cost": cost}
    verdict.setdefault("defects", [])
    return {"ok": True, "verdict": verdict, "wall_s": wall, "cost": cost,
            "num_turns": envelope.get("num_turns")}


def judge_with_retry(arm: str, tid: str, prompt: str) -> dict:
    total_cost = 0.0
    last = None
    for attempt in range(1, MAX_ATTEMPTS + 1):
        r = judge_once(prompt)
        total_cost += r.get("cost", 0.0)
        if r["ok"]:
            v = r["verdict"]
            v.update({"arm": arm, "task_id": tid, "judge_model": JUDGE_MODEL,
                      "judge_wall_s": r["wall_s"], "judge_cost_usd": round(total_cost, 6),
                      "num_turns": r.get("num_turns"), "attempts": attempt,
                      "prompt_sha256": hashlib.sha256(prompt.encode()).hexdigest()})
            return v
        last = r
        with _print_lock:
            print(f"  ! {arm}/{tid} attempt {attempt} failed "
                  f"({'retryable' if r['retryable'] else 'fatal'}): {r['err'][:120]}",
                  file=sys.stderr)
        if attempt < MAX_ATTEMPTS:
            time.sleep(RETRY_WAIT_S if r["retryable"] else 5)
    return {"strict": False, "evaluated": False,
            "defects": [f"judge-error after {MAX_ATTEMPTS} attempts: {last['err'][:200]}"],
            "judge_error": True, "arm": arm, "task_id": tid,
            "judge_model": JUDGE_MODEL, "judge_cost_usd": round(total_cost, 6),
            "attempts": MAX_ATTEMPTS,
            "prompt_sha256": hashlib.sha256(prompt.encode()).hexdigest()}


def consistency_subset(fresh_ids: list[str]) -> list[tuple[str, str]]:
    """Fixed, seed-stratified 40 (arm, task) pairs — 10 per arm."""
    rng = random.Random(CONSISTENCY_SEED)
    pairs: list[tuple[str, str]] = []
    for arm in ARMS:
        pairs += [(arm, t) for t in rng.sample(sorted(fresh_ids), CONSISTENCY_PER_ARM)]
    return pairs


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--consistency", action="store_true",
                    help="second-pass re-grade of the fixed 40-item subset")
    ap.add_argument("--retry-errors", action="store_true",
                    help="re-attempt rows whose verdict has judge_error")
    ap.add_argument("--blinding-check-only", action="store_true")
    ap.add_argument("--limit", type=int, default=0)
    args = ap.parse_args()

    tasks = load_fresh_tasks()
    fresh_ids = sorted(tasks)

    # ---- work list ---------------------------------------------------------
    runs: dict[tuple[str, str], dict] = {}
    for arm in ARMS:
        for tid in fresh_ids:
            runs[(arm, tid)] = json.loads((RUNS / arm / f"{tid}.json").read_text())

    if args.consistency:
        work = consistency_subset(fresh_ids)
        out_root = CONSISTENCY_ROOT
    else:
        work = [(a, t) for a in ARMS for t in fresh_ids]
        out_root = OUT_ROOT

    # ---- no-output rows: verdict by definition, never sent to the judge ----
    prompts: dict[tuple[str, str], str] = {}
    predecided = 0
    for arm, tid in work:
        run = runs[(arm, tid)]
        out = out_root / arm / f"{tid}.judge.json"
        if not run.get("output_lean"):
            out.parent.mkdir(parents=True, exist_ok=True)
            if not out.exists():
                out.write_text(json.dumps({
                    "strict": False, "evaluated": False,
                    "defects": ["no Lean output produced"], "no_output": True,
                    "arm": arm, "task_id": tid, "judge_model": None}, indent=1))
            predecided += 1
        else:
            prompts[(arm, tid)] = build_prompt(tasks[tid], run)

    # ---- blinding verification --------------------------------------------
    bad = blinding_scan(prompts)
    if bad:
        print("BLINDING VIOLATIONS — aborting:\n  " + "\n  ".join(bad[:20]),
              file=sys.stderr)
        return 2
    print(f"blinding scan: {len(prompts)} prompts clean "
          f"(template fields = informal/gold/produced only; "
          f"{len(_FORBIDDEN)} forbidden substrings checked); "
          f"{predecided} no-output rows pre-decided")
    if args.blinding_check_only:
        return 0

    # ---- grade -------------------------------------------------------------
    todo = []
    for (arm, tid), prompt in sorted(prompts.items()):
        out = out_root / arm / f"{tid}.judge.json"
        if out.exists():
            if args.retry_errors and json.loads(out.read_text()).get("judge_error"):
                pass  # fall through to re-grade
            else:
                continue
        todo.append((arm, tid, prompt, out))
    if args.limit:
        todo = todo[: args.limit]
    print(f"{len(todo)} rows to grade (model {JUDGE_MODEL}, concurrency {CONCURRENCY})")

    done = 0
    t0 = time.monotonic()
    with ThreadPoolExecutor(max_workers=CONCURRENCY) as ex:
        futs = {ex.submit(judge_with_retry, arm, tid, prompt): (arm, tid, out)
                for arm, tid, prompt, out in todo}
        for fut in as_completed(futs):
            arm, tid, out = futs[fut]
            v = fut.result()
            out.parent.mkdir(parents=True, exist_ok=True)
            tmp = out.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(v, indent=1, ensure_ascii=False))
            tmp.rename(out)
            done += 1
            tick = ("S" if v.get("strict") else
                    ("e" if v.get("evaluated") else
                     ("X" if v.get("judge_error") else ".")))
            with _print_lock:
                print(f"  [{done}/{len(todo)}] {arm}/{tid} {tick} "
                      f"({v.get('judge_wall_s', '?')}s)", flush=True)
    print(f"graded {done} rows in {round((time.monotonic() - t0) / 60, 1)} min "
          f"-> {out_root}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
