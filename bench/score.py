#!/usr/bin/env python3
"""Score Wikibrain benchmark results: exact-match + oracle-verified metrics.

Metrics per (arm, model, task type):
  T1  exact_gold            answered decl is in the gold accept set  [PRIMARY]
      exists_but_different  not gold, but a REAL Mathlib decl (existence oracle)
      not_found             oracle says the decl does not exist (hallucination)
  T2  qid_exact             answered QID in the gold accept set
      slug_exact            answered slug matches (sanitized compare)
      pair_exact            both                                      [PRIMARY]
  T3  accuracy              YES/NO verdict correct                    [PRIMARY]
      witness_gold          YES answers whose witness is in the gold set
      witness_valid         YES answers whose witness exists (oracle)

Existence oracle: .claude/skills/mathlib-search/mathlib_search.py decl <name>
--json (shards -> declaration-data), cached in bench/data/.decl_cache.json.

Lift = wikibrain - no_tools on the PRIMARY metric, computed on the
intersection of task ids answered by both arms, with a 95% Wald CI on the
difference of the two binomial proportions.

Output: a comparison table on stdout + bench/data/summary.json.

Examples:
  python3 bench/score.py                       # score every results_*.jsonl
  python3 bench/score.py --results bench/data/results_no_tools_claude-haiku-4-5-20251001.jsonl
  python3 bench/score.py --no-oracle           # skip existence checks (offline)
"""
from __future__ import annotations

import argparse
import json
import math
import subprocess
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from tasklib import DATA_DIR, REPO, TASKS_PATH, load_tasks, parse_answer, sanitize_slug  # noqa: E402

ORACLE = REPO / ".claude" / "skills" / "mathlib-search" / "mathlib_search.py"
DECL_CACHE = DATA_DIR / ".decl_cache.json"
SUMMARY = DATA_DIR / "summary.json"

PRIMARY = {"T1": "exact_gold", "T2": "pair_exact", "T3": "accuracy"}


# ---------------------------------------------------------------------------
# Existence oracle (cached shell-outs to mathlib_search.py)
# ---------------------------------------------------------------------------

class Oracle:
    def __init__(self, enabled: bool):
        self.enabled = enabled
        self.cache: dict[str, bool] = {}
        if DECL_CACHE.exists():
            try:
                self.cache = json.loads(DECL_CACHE.read_text())
            except json.JSONDecodeError:
                self.cache = {}
        self.dirty = False

    def exists(self, name: str | None) -> bool | None:
        """True/False from the oracle; None when disabled or the check errored."""
        if not name or not self.enabled:
            return None
        if name in self.cache:
            return self.cache[name]
        try:
            proc = subprocess.run(
                [sys.executable, str(ORACLE), "decl", name, "--json"],
                capture_output=True, text=True, timeout=120)
        except (subprocess.TimeoutExpired, OSError) as e:
            print(f"warning: oracle call failed for {name!r}: {e}", file=sys.stderr)
            return None
        try:
            val = bool(json.loads(proc.stdout).get("exists"))
        except (json.JSONDecodeError, AttributeError):
            if proc.returncode == 2:  # ApiError (network) — transient, don't cache
                print(f"warning: oracle error for {name!r}: "
                      f"{(proc.stderr or '').strip()[:200]}", file=sys.stderr)
                return None
            val = proc.returncode == 0  # cmd_decl: 0 exists / 1 not found
        self.cache[name] = val
        self.dirty = True
        return val

    def save(self) -> None:
        if self.dirty:
            tmp = DECL_CACHE.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(self.cache, indent=0, sort_keys=True))
            tmp.rename(DECL_CACHE)


# ---------------------------------------------------------------------------
# Per-row scoring
# ---------------------------------------------------------------------------

def score_row(task: dict, row: dict, oracle: Oracle) -> dict:
    typ = task["type"]
    parsed = row.get("answer_parsed")
    if parsed is None and row.get("answer_raw"):
        parsed = parse_answer(typ, row["answer_raw"])
    s: dict = {"answered": parsed is not None}
    if typ == "T1":
        decl = (parsed or {}).get("decl")
        gold = set(task["gold"]["decls"])
        s["exact_gold"] = bool(decl) and decl in gold
        if s["exact_gold"]:
            s["exists_but_different"] = False
            s["not_found"] = False
        elif decl:
            ex = oracle.exists(decl)
            s["exists_but_different"] = ex is True
            s["not_found"] = ex is False
        else:
            s["exists_but_different"] = False
            s["not_found"] = False
    elif typ == "T2":
        qid = (parsed or {}).get("qid")
        slug = (parsed or {}).get("slug")
        pairs = task["gold"]["pairs"]
        qids = {p["qid"] for p in pairs}
        s["qid_exact"] = qid in qids
        # slug compared against the matched QID's slugs when the QID is right,
        # else against every gold slug (partial credit stays mechanical).
        cand = [p for p in pairs if p["qid"] == qid] if s["qid_exact"] else pairs
        gold_slugs = {sanitize_slug(sl) for p in cand for sl in p.get("slugs", [p["slug"]])}
        s["slug_exact"] = bool(slug) and sanitize_slug(slug) in gold_slugs
        s["pair_exact"] = s["qid_exact"] and s["slug_exact"]
    elif typ == "T3":
        verdict = (parsed or {}).get("verdict")
        witness = (parsed or {}).get("witness")
        gold_yes = bool(task["gold"]["formalized"])
        s["accuracy"] = verdict == ("YES" if gold_yes else "NO")
        s["answered_yes"] = verdict == "YES"
        if verdict == "YES":
            s["witness_gold"] = bool(witness) and witness in set(task["gold"]["witness_decls"])
            s["witness_valid"] = (True if s["witness_gold"]
                                  else (oracle.exists(witness) is True))
    return s


METRICS = {
    "T1": ["exact_gold", "exists_but_different", "not_found"],
    "T2": ["qid_exact", "slug_exact", "pair_exact"],
    "T3": ["accuracy", "witness_gold", "witness_valid"],
}


def aggregate(scored: dict[str, dict]) -> dict:
    """scored: task_id -> per-row score dict (single arm). Returns type->metrics."""
    out: dict = {}
    by_type: dict[str, list] = defaultdict(list)
    for tid, s in scored.items():
        by_type[tid.split("-", 1)[0]].append(s)
    for typ, rows in sorted(by_type.items()):
        n = len(rows)
        m: dict = {"n": n, "answered": sum(1 for s in rows if s["answered"])}
        for metric in METRICS[typ]:
            # witness_* rates are over YES answers only
            if metric.startswith("witness"):
                denom = [s for s in rows if s.get("answered_yes")]
            else:
                denom = rows
            k = sum(1 for s in denom if s.get(metric) is True)
            m[metric] = {"k": k, "n": len(denom),
                         "rate": round(k / len(denom), 4) if denom else None}
        out[typ] = m
    return out


def wald_ci(k1: int, n1: int, k2: int, n2: int) -> tuple[float, float, float]:
    """(diff, lo, hi): 95% Wald CI on p2 - p1 (wikibrain minus no_tools)."""
    p1, p2 = k1 / n1, k2 / n2
    se = math.sqrt(p1 * (1 - p1) / n1 + p2 * (1 - p2) / n2)
    d = p2 - p1
    return d, d - 1.96 * se, d + 1.96 * se


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--tasks", type=Path, default=TASKS_PATH)
    ap.add_argument("--results", type=Path, nargs="*",
                    help="results files (default: bench/data/results_*.jsonl)")
    ap.add_argument("--out", type=Path, default=SUMMARY)
    ap.add_argument("--no-oracle", action="store_true",
                    help="skip decl-existence oracle calls (offline scoring)")
    args = ap.parse_args()

    tasks = {t["id"]: t for t in load_tasks(args.tasks)}
    paths = args.results or sorted(DATA_DIR.glob("results_*.jsonl"))
    if not paths:
        print("no results files found (bench/data/results_*.jsonl)", file=sys.stderr)
        return 1

    oracle = Oracle(enabled=not args.no_oracle)
    # arm_key -> task_id -> score; later rows for a task overwrite earlier
    # (retries append), error rows are skipped.
    per_arm: dict[tuple[str, str], dict[str, dict]] = defaultdict(dict)
    unknown_tasks = 0
    for path in paths:
        with open(path) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                row = json.loads(line)
                if row.get("error"):
                    continue
                task = tasks.get(row.get("task_id"))
                if task is None:
                    unknown_tasks += 1
                    continue
                key = (row.get("arm", "?"), row.get("model", "?"))
                per_arm[key][task["id"]] = score_row(task, row, oracle)
    oracle.save()
    if unknown_tasks:
        print(f"warning: {unknown_tasks} result rows reference unknown task ids "
              "(stale tasks.jsonl?)", file=sys.stderr)
    if not per_arm:
        print("no scoreable result rows", file=sys.stderr)
        return 1

    summary: dict = {"tasks_file": str(args.tasks), "arms": {}, "lift": {}}
    for (arm, model), scored in sorted(per_arm.items()):
        summary["arms"][f"{arm}/{model}"] = aggregate(scored)

    # ---- table -------------------------------------------------------------
    arm_keys = sorted(per_arm)
    col_w = max(24, *(len(f"{a}/{m}") for a, m in arm_keys)) + 2
    header = f"{'type':<5}{'metric':<22}" + "".join(
        f"{a + '/' + m:<{col_w}}" for a, m in arm_keys)
    print(header)
    print("-" * len(header))
    for typ in ("T1", "T2", "T3"):
        for metric in METRICS[typ]:
            cells = []
            for key in arm_keys:
                agg = summary["arms"][f"{key[0]}/{key[1]}"].get(typ)
                v = agg and agg.get(metric)
                cells.append(f"{v['k']}/{v['n']} ({v['rate'] * 100:.1f}%)"
                             if v and v["n"] else "—")
            mark = " *" if metric == PRIMARY[typ] else "  "
            print(f"{typ:<5}{metric + mark:<22}" + "".join(f"{c:<{col_w}}" for c in cells))
    print("(* = primary metric; witness_* rates are over YES answers)")

    # ---- lift: wikibrain - no_tools per model, intersection of task ids ----
    models = {m for a, m in arm_keys}
    for model in sorted(models):
        base = per_arm.get(("no_tools", model))
        treat = per_arm.get(("wikibrain", model))
        if not base or not treat:
            continue
        print(f"\nlift (wikibrain - no_tools) on {model}, both-arm task intersection:")
        for typ in ("T1", "T2", "T3"):
            common = sorted(t for t in set(base) & set(treat)
                            if t.split("-", 1)[0] == typ)
            if not common:
                continue
            metric = PRIMARY[typ]
            k1 = sum(1 for t in common if base[t].get(metric) is True)
            k2 = sum(1 for t in common if treat[t].get(metric) is True)
            n = len(common)
            d, lo, hi = wald_ci(k1, n, k2, n)
            summary["lift"][f"{model}/{typ}"] = {
                "metric": metric, "n": n,
                "no_tools": {"k": k1, "rate": round(k1 / n, 4)},
                "wikibrain": {"k": k2, "rate": round(k2 / n, 4)},
                "lift": round(d, 4),
                "ci95": [round(lo, 4), round(hi, 4)],
            }
            print(f"  {typ} {metric}: {k1}/{n} -> {k2}/{n}  "
                  f"lift {d * 100:+.1f}pp  (95% CI {lo * 100:+.1f} .. {hi * 100:+.1f})")

    args.out.parent.mkdir(parents=True, exist_ok=True)
    tmp = args.out.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(summary, indent=2, ensure_ascii=False) + "\n")
    tmp.rename(args.out)
    print(f"\nsummary -> {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
