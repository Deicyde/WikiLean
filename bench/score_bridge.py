#!/usr/bin/env python3
"""Score Bridge-Experiment Tier-1 runs — SKELETON with clear seams.

Grading in the Bridge Experiment is post-hoc and three-legged
(docs/research/BRIDGE-EXPERIMENT.md): (1) decl-existence / hallucinated-decl rate
of names cited in the produced Lean, (2) typecheck (pinned Lean toolchain), and
(3) a BEq+ / LLM equivalence judge vs gold. Only (1) is implemented here — the
mechanism-level signature the design calls "the cleanest". (2) and (3) are left
as EXPLICIT stub seams (typecheck_stub / judge_stub) for the toolchain- and
judge-owning agents; `success` ANDs them in the moment they return non-None.

What this computes now, per (arm, task):
  - cited decl names in output_lean (heuristic extractor)
  - each classified via the UNION oracle: declaration-data.json (416k current
    decls) ∪ catalog/data/decl_renames.jsonl (verified dead→current renames)
    -> exists | renamed | hallucinated
  - decl_existence_rate, hallucinated count/names
  - success_proxy = produced a decl AND zero hallucinated citations
    (a PLACEHOLDER for typecheck ∧ judge — labeled as such everywhere)

Aggregates: per-arm rates; the paired task_id × arm success matrix; and
McNemar-ready discordant-pair counts for the preregistered arm pairs (D-vs-E,
D-vs-C) plus every adjacent pair.

Output: bench/data/bridge_summary.json + a stdout table.

Examples:
  python3 bench/score_bridge.py                 # score bench/data/runs/*/*.json
  python3 bench/score_bridge.py --no-oracle     # skip existence (offline)
  python3 bench/score_bridge.py --selftest      # 2 hand-made fake runs
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import tempfile
from collections import defaultdict
from itertools import combinations
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from tasklib import DATA_DIR, REPO  # noqa: E402

DECL_DATA = (REPO / ".claude" / "skills" / "mathlib-search" / ".cache"
             / "declaration-data.json")
DECL_RENAMES = REPO / "catalog" / "data" / "decl_renames.jsonl"
DEFAULT_RUNS = DATA_DIR / "runs"
DEFAULT_TASKS = DATA_DIR / "bridge_tasks.jsonl"
SUMMARY = DATA_DIR / "bridge_summary.json"
ARMS = ["A", "B", "C", "D", "E"]

# Cited-name extraction is a documented heuristic (there is no Lean parser here):
# dotted identifiers whose first segment is Capitalized (Mathlib namespaces/types
# are capitalized — IsCompact.isClosed, Set.Finite) plus capitalized single
# tokens of length>=2 (IsUnit, Ring). Type variables (single uppercase letter)
# and pure syntax are excluded. Lowercase-leading dotted tokens (hs.isClosed) are
# treated as local projections, not citations. This is tuned for statement
# autoformalization, where citations are overwhelmingly capitalized types/defs.
_DOTTED_RE = re.compile(r"[A-Z][A-Za-z0-9_']*(?:\.[A-Za-z_][A-Za-z0-9_']*)+")
_SINGLE_RE = re.compile(r"\b[A-Z][A-Za-z0-9_']+\b")
_STOP = {"Type", "Prop", "Sort", "Type_", "Sortu"}


# --------------------------------------------------------------------------- #
# Union oracle                                                                  #
# --------------------------------------------------------------------------- #
class Oracle:
    def __init__(self, enabled: bool = True):
        self.enabled = enabled
        self.decls: set[str] = set()
        self.rename_cited: set[str] = set()
        self.rename_current: set[str] = set()
        if not enabled:
            return
        if DECL_DATA.exists():
            try:
                self.decls = set(json.loads(DECL_DATA.read_text())
                                 .get("declarations", {}).keys())
            except (json.JSONDecodeError, OSError) as e:
                print(f"warning: could not load decl oracle ({e}); "
                      "existence degrades to unknown", file=sys.stderr)
                self.enabled = False
        else:
            print(f"warning: {DECL_DATA} missing; run with --no-oracle or fetch it",
                  file=sys.stderr)
            self.enabled = False
        if DECL_RENAMES.exists():
            for line in DECL_RENAMES.read_text().splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    r = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if "_meta" in r:
                    continue
                if r.get("cited"):
                    self.rename_cited.add(r["cited"])
                if r.get("current"):
                    self.rename_current.add(r["current"])

    def classify(self, name: str) -> str:
        """exists | renamed | hallucinated | unknown(oracle off)."""
        if not self.enabled:
            return "unknown"
        if name in self.decls or name in self.rename_current:
            return "exists"
        if name in self.rename_cited:
            return "renamed"  # existed historically; a known dead-but-mappable name
        return "hallucinated"


def extract_cited(lean: str | None) -> list[str]:
    if not lean:
        return []
    names: list[str] = []
    seen: set[str] = set()
    dotted = list(_DOTTED_RE.finditer(lean))
    for m in dotted:
        nm = m.group(0)
        if nm not in seen:
            seen.add(nm)
            names.append(nm)
    # single capitalized tokens NOT already covered as a dotted segment
    remainder = _DOTTED_RE.sub(" ", lean)
    for m in _SINGLE_RE.finditer(remainder):
        nm = m.group(0)
        if nm in _STOP or len(nm) < 2 or nm in seen:
            continue
        seen.add(nm)
        names.append(nm)
    return names


# --------------------------------------------------------------------------- #
# Stub seams — implemented by the toolchain-owning and judge-owning agents.      #
# Each returns None ("not available"); `success` folds them in when they don't. #
# --------------------------------------------------------------------------- #
_TC_ENV = None
_MAIN_SOCK = "/tmp/wikilean_tc.sock"
_FRESH_SOCK = "/tmp/wikilean_tc_fresh.sock"
_FRESH_PROJECT = "/Users/jack/Desktop/LEAN/bench-lean-fresh"
_TC_ENV_FRESH = None

def _route_for(task_id: str) -> tuple[str | None, str | None]:
    """Fresh-set rows grade on the fresh pin (theorems NEWER than the Tier-1a pin
    — that is the held-out design, docs/research/BRIDGE-EXPERIMENT.md deviations).
    Everything else grades on the default pin — through its server socket when one
    is up (a warm single-shot is ~60s vs seconds on the server, and 150 rows of
    single-shot is how a scoring pass silently takes hours)."""
    import os
    if task_id.startswith("fresh_") and os.path.exists(_FRESH_SOCK):
        return _FRESH_SOCK, _FRESH_PROJECT
    if os.path.exists(_MAIN_SOCK):
        return _MAIN_SOCK, None
    return None, None

def typecheck_stub(task: dict, run: dict) -> bool | None:
    """Wired to the sibling rig (bench/typecheck.py): compile gold_header +
    output_lean against the pinned toolchain. `sorry` is a warning, not an error,
    so a statement-only decl is expected to come back ok. Returns None only when
    there is no output to check."""
    global _TC_ENV, _TC_ENV_FRESH
    if not run.get("output_lean"):
        return None
    import typecheck as _tcmod
    import os
    sock, project = _route_for(task.get("id") or run.get("task_id") or "")
    if project:  # fresh pin
        if _TC_ENV_FRESH is None:
            _TC_ENV_FRESH = _tcmod.resolve_env(Path(project))
        tc_env = _TC_ENV_FRESH
    else:  # default (Tier-1a) pin, with or without its server
        if _TC_ENV is None:
            _TC_ENV = _tcmod.resolve_env(Path(_tcmod.DEFAULT_PROJECT))
        tc_env = _TC_ENV
    prev = os.environ.get("BENCH_TC_SERVER")
    if sock:
        os.environ["BENCH_TC_SERVER"] = sock
    from construct import prepare_candidate
    code = prepare_candidate(run["output_lean"], task.get("gold_header") or "")
    r = _tcmod.typecheck(code, tc_env, timeout=90,
                         max_workers=_tcmod.auto_workers() if hasattr(_tcmod, "auto_workers") else 4,
                         wait_timeout=900)
    if sock:
        if prev is None:
            os.environ.pop("BENCH_TC_SERVER", None)
        else:
            os.environ["BENCH_TC_SERVER"] = prev
    run["_typecheck"] = {k: r[k] for k in ("ok", "elapsed_s", "timed_out")}
    run["_typecheck"]["errors"] = [e.get("message", "")[:200] for e in r.get("errors", [])][:4]
    return bool(r["ok"])


def judge_stub(task: dict, run: dict) -> bool | None:
    """TODO(judge): BEq+ / LLM equivalence of output_lean vs task['gold_formal']
    (calibrated on 50 human-graded items first; report judge–human agreement)."""
    return None


# --------------------------------------------------------------------------- #
# Per-run scoring                                                               #
# --------------------------------------------------------------------------- #
def score_run(task: dict, run: dict, oracle: Oracle) -> dict:
    lean = run.get("output_lean")
    errored = bool(run.get("error"))
    cited = extract_cited(lean)
    cls = {n: oracle.classify(n) for n in cited}
    exists = [n for n, c in cls.items() if c == "exists"]
    renamed = [n for n, c in cls.items() if c == "renamed"]
    hallucinated = [n for n, c in cls.items() if c == "hallucinated"]
    resolved = len(exists) + len(renamed)
    denom = resolved + len(hallucinated)  # 'unknown' (oracle off) excluded
    tc = typecheck_stub(task, run)
    jd = judge_stub(task, run)
    # success_proxy: produced a decl, no error, zero hallucinated citations.
    # Replace with `proxy AND tc AND jd` once the stubs return non-None.
    proxy = bool(lean) and not errored and len(hallucinated) == 0
    success = proxy
    for leg in (tc, jd):
        if leg is not None:
            success = success and leg
    return {
        "produced": bool(lean), "errored": errored,
        "n_cited": len(cited), "n_exists": len(exists), "n_renamed": len(renamed),
        "n_hallucinated": len(hallucinated), "hallucinated_names": hallucinated,
        "decl_existence_rate": (round(resolved / denom, 4) if denom else None),
        "typecheck": tc, "judge": jd,
        "typecheck_timed_out": bool((run.get("_typecheck") or {}).get("timed_out")),
        "success_proxy": proxy, "success": success,
        "stats": run.get("transcript_stats") or {},
    }


def load_tasks(path: Path) -> dict[str, dict]:
    tasks: dict[str, dict] = {}
    if not path.exists():
        return tasks
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        r = json.loads(line)
        if "_meta" in r:
            continue
        tasks[r["id"]] = r
    return tasks


def load_runs(runs_dir: Path) -> dict[str, dict[str, dict]]:
    """arm -> task_id -> run row."""
    out: dict[str, dict[str, dict]] = defaultdict(dict)
    if not runs_dir.exists():
        return out
    for armdir in sorted(runs_dir.iterdir()):
        if not armdir.is_dir():
            continue
        for f in sorted(armdir.glob("*.json")):
            try:
                row = json.loads(f.read_text())
            except (json.JSONDecodeError, OSError):
                continue
            tid = row.get("task_id") or f.stem
            out[armdir.name][tid] = row
    return out


# --------------------------------------------------------------------------- #
# Aggregation                                                                   #
# --------------------------------------------------------------------------- #
def aggregate_arm(scored: dict[str, dict]) -> dict:
    n = len(scored)
    if not n:
        return {"n": 0}
    produced = sum(1 for s in scored.values() if s["produced"])
    succ = sum(1 for s in scored.values() if s["success_proxy"])
    succ_full = sum(1 for s in scored.values() if s["success"])
    tc_ok = sum(1 for s in scored.values() if s["typecheck"] is True)
    tc_none = sum(1 for s in scored.values() if s["typecheck"] is None)
    tc_to = sum(1 for s in scored.values() if s.get("typecheck_timed_out"))
    hall_runs = sum(1 for s in scored.values() if s["n_hallucinated"] > 0)
    cited = sum(s["n_cited"] for s in scored.values())
    hall = sum(s["n_hallucinated"] for s in scored.values())
    rates = [s["decl_existence_rate"] for s in scored.values()
             if s["decl_existence_rate"] is not None]
    calls = [sum((s["stats"].get("tool_calls_by_name") or {}).values())
             for s in scored.values()]
    tin = [s["stats"].get("tokens_in") for s in scored.values()
           if s["stats"].get("tokens_in") is not None]
    tout = [s["stats"].get("tokens_out") for s in scored.values()
            if s["stats"].get("tokens_out") is not None]
    tool_by_name: dict[str, int] = defaultdict(int)
    for s in scored.values():
        for k, v in (s["stats"].get("tool_calls_by_name") or {}).items():
            tool_by_name[k] += v
    return {
        "n": n, "produced": produced,
        "success_proxy_k": succ,
        "success_proxy_rate": round(succ / n, 4),
        "success_k": succ_full,
        "success_rate": round(succ_full / n, 4),
        "typecheck_ok_k": tc_ok, "typecheck_none_k": tc_none,
        "typecheck_timeout_k": tc_to,
        "runs_with_hallucination": hall_runs,
        "cited_total": cited, "hallucinated_total": hall,
        "hallucinated_decl_rate": round(hall / cited, 4) if cited else None,
        "mean_decl_existence_rate": round(sum(rates) / len(rates), 4) if rates else None,
        "mean_tool_calls": round(sum(calls) / n, 2),
        "tool_calls_by_name": dict(sorted(tool_by_name.items())),
        "mean_tokens_out": round(sum(tout) / len(tout), 1) if tout else None,
        "mean_tokens_in": round(sum(tin) / len(tin), 1) if tin else None,
    }


def mcnemar(scored_x: dict[str, dict], scored_y: dict[str, dict]) -> dict:
    """2×2 discordant-pair counts on tasks BOTH arms attempted (success is bool)."""
    common = sorted(set(scored_x) & set(scored_y))
    both = xonly = yonly = neither = 0
    for t in common:
        sx, sy = scored_x[t]["success"], scored_y[t]["success"]
        if sx and sy:
            both += 1
        elif sx and not sy:
            xonly += 1
        elif sy and not sx:
            yonly += 1
        else:
            neither += 1
    b, c = xonly, yonly  # discordant pairs
    return {"n_paired": len(common), "both_success": both,
            "x_only": b, "y_only": c, "neither": neither,
            "discordant": b + c,
            "note": "McNemar b=x_only, c=y_only on `success` "
                    "(proxy ∧ typecheck when available; judge pending calibration)"}


# --------------------------------------------------------------------------- #
# main                                                                          #
# --------------------------------------------------------------------------- #
def run_scoring(runs_dir: Path, tasks_path: Path, oracle: Oracle,
                out_path: Path | None) -> dict:
    tasks = load_tasks(tasks_path)
    runs = load_runs(runs_dir)
    present = [a for a in ARMS if a in runs] + [a for a in runs if a not in ARMS]

    scored: dict[str, dict[str, dict]] = {}
    for arm in present:
        scored[arm] = {}
        for tid, row in runs[arm].items():
            scored[arm][tid] = score_run(tasks.get(tid, {}), row, oracle)

    tc_any = any(s["typecheck"] is not None
                 for a in present for s in scored[a].values())
    summary: dict = {
        "runs_dir": str(runs_dir), "tasks_file": str(tasks_path),
        "oracle_enabled": oracle.enabled,
        "success_metric": ("produced ∧ no-halluc ∧ TYPECHECK (judge pending "
                           "calibration)" if tc_any else
                           "success_proxy (produced ∧ no hallucinated citations) — "
                           "PLACEHOLDER; typecheck unavailable, judge stub None"),
        "arms": {a: aggregate_arm(scored[a]) for a in present},
        "paired_matrix": {}, "mcnemar": {},
    }
    # paired task_id × arm success matrix
    all_tids = sorted({t for a in present for t in scored[a]})
    for tid in all_tids:
        summary["paired_matrix"][tid] = {
            a: scored[a][tid]["success"] if tid in scored[a] else None
            for a in present}
    # McNemar: preregistered pairs first, then every adjacent pair present
    pairs = [("D", "E"), ("D", "C")] + list(combinations(present, 2))
    seen: set[tuple[str, str]] = set()
    for x, y in pairs:
        if x in scored and y in scored and (x, y) not in seen:
            seen.add((x, y))
            summary["mcnemar"][f"{x}_vs_{y}"] = mcnemar(scored[x], scored[y])

    if out_path is not None:
        out_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = out_path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(summary, indent=2, ensure_ascii=False) + "\n")
        tmp.rename(out_path)
    return summary


def print_table(summary: dict) -> None:
    arms = list(summary["arms"])
    if not arms:
        print("no run files found.")
        return
    print(f"\nBridge Tier-1 — {summary['runs_dir']}"
          f"  (oracle {'ON' if summary['oracle_enabled'] else 'OFF'})")
    print(f"success metric: {summary['success_metric']}\n")
    rows = [
        ("n runs", lambda a: a["n"]),
        ("produced decl", lambda a: a["produced"]),
        ("success_proxy", lambda a: f"{a['success_proxy_k']}/{a['n']} "
                                    f"({a['success_proxy_rate'] * 100:.1f}%)"),
        ("success (folded)", lambda a: f"{a['success_k']}/{a['n']} "
                                       f"({a['success_rate'] * 100:.1f}%)"),
        ("typecheck ok", lambda a: f"{a['typecheck_ok_k']}/{a['n']}"
                                   + (f" (to={a['typecheck_timeout_k']})"
                                      if a.get('typecheck_timeout_k') else "")),
        ("halluc-decl rate", lambda a: (f"{a['hallucinated_total']}/{a['cited_total']} "
                                        f"({a['hallucinated_decl_rate'] * 100:.1f}%)"
                                        if a.get('hallucinated_decl_rate') is not None
                                        else "—")),
        ("runs w/ halluc", lambda a: a["runs_with_hallucination"]),
        ("mean tool calls", lambda a: a["mean_tool_calls"]),
        ("mean tokens_out", lambda a: a["mean_tokens_out"]),
    ]
    w = 18
    print(f"{'metric':<20}" + "".join(f"{('arm ' + a):<{w}}" for a in arms))
    print("-" * (20 + w * len(arms)))
    for label, fn in rows:
        cells = []
        for a in arms:
            agg = summary["arms"][a]
            cells.append(str(fn(agg)) if agg.get("n") else "—")
        print(f"{label:<20}" + "".join(f"{c:<{w}}" for c in cells))
    if summary["mcnemar"]:
        print("\nMcNemar-ready discordant pairs (success, tc-folded):")
        for pair, m in summary["mcnemar"].items():
            print(f"  {pair}: n={m['n_paired']} both={m['both_success']} "
                  f"b(x_only)={m['x_only']} c(y_only)={m['y_only']} "
                  f"neither={m['neither']} discordant={m['discordant']}")
    print(f"\n(success folds in: {summary['success_metric']})")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--runs-dir", type=Path, default=DEFAULT_RUNS)
    ap.add_argument("--tasks", type=Path, default=DEFAULT_TASKS)
    ap.add_argument("--out", type=Path, default=SUMMARY)
    ap.add_argument("--no-oracle", action="store_true",
                    help="skip the decl-existence oracle (offline)")
    ap.add_argument("--selftest", action="store_true",
                    help="score 2 hand-made fake run files and exit")
    args = ap.parse_args()

    if args.selftest:
        return selftest(no_oracle=args.no_oracle)

    oracle = Oracle(enabled=not args.no_oracle)
    summary = run_scoring(args.runs_dir, args.tasks, oracle, args.out)
    print_table(summary)
    print(f"\nsummary -> {args.out}")
    if not summary["arms"]:
        print(f"(no runs under {args.runs_dir}; run bench/run_bridge.py first)",
              file=sys.stderr)
        return 1
    return 0


# --------------------------------------------------------------------------- #
# Self-test: 2 hand-made fake run files across arms D and E                      #
# --------------------------------------------------------------------------- #
def selftest(no_oracle: bool = False) -> int:
    tmp = Path(tempfile.mkdtemp(prefix="score-bridge-selftest-"))
    tasks_p = tmp / "bridge_tasks.jsonl"
    tasks_p.write_text("\n".join([
        json.dumps({"_meta": {"note": "selftest"}}),
        json.dumps({"id": "T-001", "informal_statement": "compact ⊆ Hausdorff ⇒ closed",
                    "gold_formal": "theorem IsCompact.isClosed ... := sorry",
                    "gold_header": "import Mathlib\n", "split": "dev", "domain": "top"}),
        json.dumps({"id": "T-002", "informal_statement": "nilpotent ⇒ 1+x unit",
                    "gold_formal": "theorem foo ... := sorry",
                    "gold_header": "import Mathlib\n", "split": "dev", "domain": "alg"}),
    ]) + "\n")
    runs = tmp / "runs"
    # Arm D: T-001 clean (real decls), T-002 cites a hallucinated name.
    (runs / "D").mkdir(parents=True)
    (runs / "D" / "T-001.json").write_text(json.dumps({
        "task_id": "T-001", "arm": "D", "model": "m",
        "output_lean": "theorem c_closed {X : Type*} [TopologicalSpace X] [T2Space X]\n"
                       "  {s : Set X} (hs : IsCompact s) : IsClosed s := sorry",
        "transcript_stats": {"turns": 4, "tool_calls_by_name": {"brain_transfer": 2},
                             "tokens_in": 1200, "tokens_out": 300, "cost_usd": 0.01},
        "wall_s": 12.3, "timestamp": "2026-07-16T00:00:00Z"}))
    (runs / "D" / "T-002.json").write_text(json.dumps({
        "task_id": "T-002", "arm": "D", "model": "m",
        "output_lean": "theorem u {R : Type*} [Ring R] {x : R} (hx : IsNilpotent x) :\n"
                       "  IsUnit (1 + x) := Totally.Made.Up := sorry",
        "transcript_stats": {"turns": 6, "tool_calls_by_name": {"brain_transfer": 3},
                             "tokens_in": 2000, "tokens_out": 500, "cost_usd": 0.02},
        "wall_s": 20.1, "timestamp": "2026-07-16T00:00:00Z"}))
    # Arm E: T-001 hallucinates, T-002 clean — flips both discordant cells vs D.
    (runs / "E").mkdir(parents=True)
    (runs / "E" / "T-001.json").write_text(json.dumps({
        "task_id": "T-001", "arm": "E", "model": "m",
        "output_lean": "theorem c {X : Type*} (hs : IsCompact s) : Nonexistent.Decl s := sorry",
        "transcript_stats": {"turns": 8, "tool_calls_by_name": {"decl_grep": 4, "wiki_get": 1},
                             "tokens_in": 3000, "tokens_out": 400, "cost_usd": 0.03},
        "wall_s": 30.0, "timestamp": "2026-07-16T00:00:00Z"}))
    (runs / "E" / "T-002.json").write_text(json.dumps({
        "task_id": "T-002", "arm": "E", "model": "m",
        "output_lean": "theorem u {R : Type*} [Ring R] {x : R} (hx : IsNilpotent x) :\n"
                       "  IsUnit (1 + x) := sorry",
        "transcript_stats": {"turns": 5, "tool_calls_by_name": {"loogle": 2},
                             "tokens_in": 1500, "tokens_out": 260, "cost_usd": 0.015},
        "wall_s": 18.0, "timestamp": "2026-07-16T00:00:00Z"}))

    oracle = Oracle(enabled=not no_oracle)
    summary = run_scoring(runs, tasks_p, oracle, tmp / "bridge_summary.json")
    print_table(summary)

    # Assertions (only when the oracle is on — existence is what we're checking).
    if oracle.enabled:
        d, e = summary["arms"]["D"], summary["arms"]["E"]
        assert d["hallucinated_total"] == 1, d
        assert e["hallucinated_total"] == 1, e
        m = summary["mcnemar"]["D_vs_E"]
        assert m["n_paired"] == 2, m
        assert m["x_only"] == 1 and m["y_only"] == 1, m  # fully discordant
        assert d["success_proxy_k"] == 1 and e["success_proxy_k"] == 1, (d, e)
        print("\nscore_bridge selftest OK (D & E each 1 halluc, D_vs_E fully discordant)")
    else:
        print("\nscore_bridge selftest ran (oracle OFF — existence assertions skipped)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
