#!/usr/bin/env python3
"""REVIEW-2 §7b — validate the hallucination oracle + run-level rates.

Responds to docs/research/review/REVIEW-2.md §7: "Validate the regex/oracle on a
blinded sample and give a paired run-level comparison for 'any hallucination'
(the current 'roughly 3x' applies to citation-level rates, and citations
cluster within runs)."

Data: the 500 post-repair fresh rows bench/data/runs/{A..E}/fresh_*.json
(the 31 arm-E reruns of 2026-07-27 included — verified against
bench/analysis/snapshot_fresh_orig/MANIFEST.json provenance chain).
Classifier under test: bench/score_bridge.py `extract_cited` (regex) +
`Oracle` (doc-gen4 declaration-data.json UNION catalog/data/decl_renames.jsonl).

Subcommands (in the order the protocol runs them):

  table     Part 2 — run-level any-hallucination table for all 5 arms
            (rate + Wilson 95% CI) + paired exact McNemar for D-vs-E, D-vs-C
            (and the remaining D pairs + E-vs-C for context).
            Merges results into halluc_validation.json.

  sample    Part 1 step 1 — SEEDED (--seed, default 20260801) stratified
            sample of 60 distinct cited names across arm x oracle-verdict
            strata. Writes:
              halluc_blind/blinded_sample.json   (NO verdicts; shuffled)
              halluc_blind/sample_key.json       (verdicts; sealed until compare)
            Blinding protocol: the grader reads ONLY blinded_sample.json and
            evidence.json, determines ground truth for every item, writes
            halluc_blind/truth_labels.json, and only then runs `compare`.

  evidence  Part 1 step 2 — for each blinded item, oracle-free evidence from
            the pinned Mathlib tree (git grep at 61a5e4f338, the rev the
            agents' file tools saw; cross-grep at 9944fe2973, the fresh
            typecheck env's mathlib rev). No oracle lookups here.
            Writes halluc_blind/evidence.json.

  compare   Part 1 step 3 — join truth_labels.json with sample_key.json;
            confusion matrix, precision/recall on the hallucinated and real
            (exists|renamed) classes, exact error cases.
            Merges results into halluc_validation.json.

Truth label vocabulary (recorded per item in truth_labels.json):
  real            exists at the pin (or in the pinned toolchain's core/deps)
                  under exactly the cited (fully-qualified or root) name
  renamed         does not exist at the pin, but is a former Mathlib name with
                  a verified current equivalent
  nonexistent     declaration-shaped citation with no such declaration at the
                  pin (a true hallucination)
  not_a_citation  extractor noise — the token is not a declaration citation at
                  all (comment prose, local variable, notation fragment)
For binary scoring, truth-positive ("hallucinated") = nonexistent; truth
not_a_citation items are reported both ways (strict: FP for the oracle;
lenient: excluded) because they measure the *extractor*, not the oracle.

Everything is deterministic given --seed. No network. Never git-commits.
"""
from __future__ import annotations

import argparse
import json
import math
import random
import re
import subprocess
import sys
from collections import defaultdict
from pathlib import Path

HERE = Path(__file__).resolve().parent          # bench/analysis
BENCH = HERE.parent                              # bench
REPO = BENCH.parent                              # WikiLean
sys.path.insert(0, str(BENCH))
from score_bridge import Oracle, extract_cited  # noqa: E402

RUNS = BENCH / "data" / "runs"
ARMS = ["A", "B", "C", "D", "E"]
OUT_JSON = HERE / "halluc_validation.json"
BLIND_DIR = HERE / "halluc_blind"
MATHLIB = Path("/Users/jack/Desktop/LEAN/mathlib4")
PIN_AGENT = "61a5e4f338bfdddf2f6296402a49fe80f3b1a147"   # agents' file-tool tree
PIN_TYPECHECK = "9944fe2973b8dc0b86949101ed98232c07cd54a0"  # bench-lean-fresh mathlib rev

DEFAULT_SEED = 20260801
N_SAMPLE = 60
# per-arm quotas over oracle verdict strata (sum = 12/arm, 60 total).
# renamed is rare; shortfalls fall back to exists.
QUOTA = {"hallucinated": 6, "exists": 5, "renamed": 1}

DECL_KEYWORDS = ("theorem|lemma|def|abbrev|structure|class|inductive|instance|"
                 "opaque|axiom|abbreviation")


# --------------------------------------------------------------------------- #
# shared loading                                                              #
# --------------------------------------------------------------------------- #
def load_fresh_rows() -> dict[str, dict[str, dict]]:
    """arm -> task_id -> run row (post-repair fresh set, 100 rows/arm)."""
    out: dict[str, dict[str, dict]] = {}
    for arm in ARMS:
        rows = {}
        for f in sorted((RUNS / arm).glob("fresh_*.json")):
            r = json.loads(f.read_text())
            rows[r["task_id"]] = r
        assert len(rows) == 100, f"arm {arm}: expected 100 fresh rows, got {len(rows)}"
        out[arm] = rows
    return out


def classify_all(rows_by_arm, oracle):
    """-> per-citation records + per-run any-halluc flags.

    citation record: (arm, task_id, name, verdict, context_line)
    """
    citations = []
    run_flags: dict[str, dict[str, bool | None]] = {a: {} for a in ARMS}
    for arm in ARMS:
        for tid, r in rows_by_arm[arm].items():
            lean = r.get("output_lean")
            names = extract_cited(lean)
            any_h = False
            for n in names:
                v = oracle.classify(n)
                ctx = ""
                if lean:
                    for line in lean.splitlines():
                        if n in line:
                            ctx = line.strip()[:160]
                            break
                citations.append(
                    {"arm": arm, "task_id": tid, "name": n, "verdict": v,
                     "context": ctx})
                if v == "hallucinated":
                    any_h = True
            # produced=False rows have no citations -> any_h False (they also
            # cannot hallucinate; kept in the n=100 denominator, as in the draft)
            run_flags[arm][tid] = any_h
    return citations, run_flags


# --------------------------------------------------------------------------- #
# stats helpers                                                               #
# --------------------------------------------------------------------------- #
def wilson95(k: int, n: int) -> tuple[float, float]:
    if n == 0:
        return (0.0, 0.0)
    z = 1.959963984540054
    p = k / n
    denom = 1 + z * z / n
    center = (p + z * z / (2 * n)) / denom
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    return (round(center - half, 4), round(center + half, 4))


def mcnemar_exact(b: int, c: int) -> float:
    """two-sided exact binomial p for discordant counts b, c."""
    n = b + c
    if n == 0:
        return 1.0
    k = min(b, c)
    tail = sum(math.comb(n, i) for i in range(0, k + 1)) / 2 ** n
    return float(f"{min(1.0, 2 * tail):.3g}")


# --------------------------------------------------------------------------- #
# Part 2 — run-level table                                                    #
# --------------------------------------------------------------------------- #
def cmd_table(_args) -> None:
    oracle = Oracle()
    assert oracle.enabled, "oracle sources missing"
    rows = load_fresh_rows()
    citations, run_flags = classify_all(rows, oracle)

    per_arm = {}
    for arm in ARMS:
        cited = [c for c in citations if c["arm"] == arm]
        hall = [c for c in cited if c["verdict"] == "hallucinated"]
        k = sum(1 for v in run_flags[arm].values() if v)
        n = len(run_flags[arm])
        per_arm[arm] = {
            "n_runs": n,
            "runs_any_halluc": k,
            "run_rate": round(k / n, 4),
            "run_wilson95": wilson95(k, n),
            "citations_total": len(cited),
            "citations_halluc": len(hall),
            "citation_rate": round(len(hall) / len(cited), 4) if cited else None,
        }

    pairs = {}
    for x, y in [("D", "E"), ("D", "C"), ("D", "A"), ("D", "B"), ("E", "C")]:
        tids = sorted(set(run_flags[x]) & set(run_flags[y]))
        b = sum(1 for t in tids if run_flags[x][t] and not run_flags[y][t])
        c = sum(1 for t in tids if not run_flags[x][t] and run_flags[y][t])
        both = sum(1 for t in tids if run_flags[x][t] and run_flags[y][t])
        neither = len(tids) - b - c - both
        rd = (sum(run_flags[x].values()) - sum(run_flags[y].values())) / len(tids)
        # paired Wald CI for the rate difference
        n = len(tids)
        se = math.sqrt((b + c) / n**2 - (b - c) ** 2 / n**3) if n else 0.0
        pairs[f"{x}_vs_{y}"] = {
            "n_paired": n,
            f"both_halluc": both, f"{x}_only": b, f"{y}_only": c,
            "neither": neither, "discordant": b + c,
            "p_mcnemar_exact_two_sided": mcnemar_exact(b, c),
            "rate_diff": round(rd, 4),
            "rate_diff_wald95": [round(rd - 1.959964 * se, 4),
                                 round(rd + 1.959964 * se, 4)],
        }

    ratios = {
        "run_level_E_over_D": round(per_arm["E"]["run_rate"] / per_arm["D"]["run_rate"], 2),
        "run_level_C_over_D": round(per_arm["C"]["run_rate"] / per_arm["D"]["run_rate"], 2),
        "citation_level_E_over_D": round(per_arm["E"]["citation_rate"] / per_arm["D"]["citation_rate"], 2),
        "citation_level_C_over_D": round(per_arm["C"]["citation_rate"] / per_arm["D"]["citation_rate"], 2),
    }

    merge_json({"provenance": provenance_block(),
                "run_level": {"per_arm": per_arm, "mcnemar_pairs": pairs,
                              "ratio_summary": ratios}})
    print(json.dumps({"per_arm": per_arm, "mcnemar_pairs": pairs,
                      "ratio_summary": ratios}, indent=1))


# --------------------------------------------------------------------------- #
# Part 1 step 1 — seeded blinded sample                                       #
# --------------------------------------------------------------------------- #
def cmd_sample(args) -> None:
    oracle = Oracle()
    assert oracle.enabled, "oracle sources missing"
    rows = load_fresh_rows()
    citations, _ = classify_all(rows, oracle)

    # distinct names per arm x verdict (oracle verdict is a pure function of the
    # name, so validating a name twice adds nothing)
    strata: dict[tuple[str, str], list[dict]] = defaultdict(list)
    seen: set[tuple[str, str]] = set()
    for c in citations:
        key = (c["arm"], c["name"])
        if key in seen:
            continue
        seen.add(key)
        strata[(c["arm"], c["verdict"])].append(c)

    rng = random.Random(args.seed)
    picked: list[dict] = []
    for arm in ARMS:
        arm_quota = dict(QUOTA)
        # renamed shortfall -> exists; hallucinated shortfall -> exists
        for verdict in ("hallucinated", "renamed", "exists"):
            pool = sorted(strata.get((arm, verdict), []),
                          key=lambda c: (c["task_id"], c["name"]))
            want = arm_quota[verdict]
            if len(pool) < want:
                arm_quota["exists"] += want - len(pool)
                want = len(pool)
            picked.extend(rng.sample(pool, want) if want else [])
    assert len(picked) == N_SAMPLE, f"sample size {len(picked)} != {N_SAMPLE}"

    rng.shuffle(picked)
    BLIND_DIR.mkdir(exist_ok=True)
    blinded = [{"i": i, "arm": c["arm"], "task_id": c["task_id"],
                "name": c["name"], "context": c["context"]}
               for i, c in enumerate(picked)]
    key = [{"i": i, "arm": c["arm"], "task_id": c["task_id"],
            "name": c["name"], "oracle_verdict": c["verdict"]}
           for i, c in enumerate(picked)]
    (BLIND_DIR / "blinded_sample.json").write_text(json.dumps(
        {"seed": args.seed, "n": N_SAMPLE, "quota_per_arm": QUOTA,
         "population": "distinct (arm, name) citations, post-repair fresh rows",
         "items": blinded}, indent=1))
    (BLIND_DIR / "sample_key.json").write_text(json.dumps(
        {"seed": args.seed, "SEALED": "do not read before truth_labels.json is complete",
         "items": key}, indent=1))
    print(f"wrote {BLIND_DIR}/blinded_sample.json (+ sealed sample_key.json), "
          f"seed={args.seed}")
    counts = defaultdict(int)
    for c in picked:
        counts[c["verdict"]] += 1
    print("strata drawn (for the record, aggregate only):", dict(counts))


# --------------------------------------------------------------------------- #
# Part 1 step 2 — oracle-free pinned-tree evidence                            #
# --------------------------------------------------------------------------- #
def _git_grep(pattern: str, rev: str, max_lines: int = 8,
              fixed_word: bool = False) -> list[str]:
    """git grep at a rev. POSIX ERE only — no \\s or \\b (use [[:space:]] and
    explicit non-ident guards); fixed_word=True does -wF whole-word literal."""
    cmd = ["git", "-C", str(MATHLIB), "grep", "-n"]
    cmd += ["-wF"] if fixed_word else ["-E"]
    cmd += [pattern, rev, "--", "Mathlib"]
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    except subprocess.TimeoutExpired:
        return ["<grep timeout>"]
    lines = p.stdout.splitlines()
    return lines[:max_lines] + ([f"... ({len(lines)} total hits)"]
                                if len(lines) > max_lines else [])


_MODS = "(protected |private |noncomputable |scoped )*"


def _decl_head_pat(seg: str, dotted_ok: bool = False) -> str:
    guard = "[^A-Za-z0-9_']" if dotted_ok else "[^A-Za-z0-9_'.]"
    return (rf"^[[:space:]]*{_MODS}({DECL_KEYWORDS})[[:space:]]+"
            rf"{re.escape(seg)}({guard}|$)")


def evidence_for(name: str) -> dict:
    """Raw pinned-tree evidence for one cited name. NO oracle lookups."""
    last = name.rsplit(".", 1)[-1]
    ev = {"name": name, "pin": PIN_AGENT}
    # declaration-head lines for the exact last segment (no dotted extension)
    ev["decl_head_hits"] = _git_grep(_decl_head_pat(last), PIN_AGENT)
    # decl heads that *extend* the name (namespace evidence: `def Name.foo`)
    ev["decl_head_dotted_hits"] = _git_grep(
        rf"^[[:space:]]*{_MODS}({DECL_KEYWORDS})[[:space:]]+{re.escape(name)}\.",
        PIN_AGENT, max_lines=4)
    # any whole-word occurrence of the full (dotted) name — usage evidence
    ev["full_name_hits"] = _git_grep(name, PIN_AGENT, max_lines=5, fixed_word=True)
    if "." in name:
        prefix = name.rsplit(".", 1)[0]
        ev["namespace_hits"] = _git_grep(
            rf"^[[:space:]]*namespace[[:space:]]+{re.escape(prefix)}([^A-Za-z0-9_']|$)",
            PIN_AGENT, max_lines=4)
    # cross-check at the typecheck env's mathlib rev only when the pin missed
    if not ev["decl_head_hits"] and not ev["full_name_hits"]:
        ev["typecheck_rev_decl_head_hits"] = _git_grep(
            _decl_head_pat(last), PIN_TYPECHECK)
    return ev


def cmd_evidence(_args) -> None:
    blinded = json.loads((BLIND_DIR / "blinded_sample.json").read_text())
    out = []
    for item in blinded["items"]:
        ev = evidence_for(item["name"])
        ev["i"] = item["i"]
        out.append(ev)
        print(f"[{item['i']:2d}] {item['name']}: "
              f"{len([h for h in ev['decl_head_hits'] if not h.startswith('...')])} decl-head, "
              f"{len([h for h in ev['full_name_hits'] if not h.startswith('...')])} full-name hits")
    (BLIND_DIR / "evidence.json").write_text(json.dumps(out, indent=1))
    print(f"wrote {BLIND_DIR}/evidence.json")


# --------------------------------------------------------------------------- #
# Part 1 step 3 — unblind + compare                                           #
# --------------------------------------------------------------------------- #
def cmd_compare(_args) -> None:
    labels_p = BLIND_DIR / "truth_labels.json"
    assert labels_p.exists(), ("truth_labels.json missing — grade the blinded "
                               "sample first (protocol order is enforced)")
    truth = {t["i"]: t for t in json.loads(labels_p.read_text())["items"]}
    key = {k["i"]: k for k in
           json.loads((BLIND_DIR / "sample_key.json").read_text())["items"]}
    assert set(truth) == set(key), "truth labels do not cover the sample"

    # binary mapping: oracle-positive = 'hallucinated'; truth-positive = 'nonexistent'
    rows = []
    for i in sorted(key):
        o = key[i]["oracle_verdict"]
        t = truth[i]["truth"]
        rows.append({"i": i, "name": key[i]["name"], "arm": key[i]["arm"],
                     "task_id": key[i]["task_id"], "oracle": o, "truth": t,
                     "note": truth[i].get("note", "")})

    def stats(exclude_noise: bool):
        tp = fp = fn = tn = 0
        errors = []
        for r in rows:
            if exclude_noise and r["truth"] == "not_a_citation":
                continue
            opos = r["oracle"] == "hallucinated"
            tpos = r["truth"] in (("nonexistent",) if exclude_noise
                                  else ("nonexistent", "not_a_citation"))
            # strict mode: a not_a_citation token flagged 'hallucinated' counts
            # as an oracle FALSE POSITIVE for the hallucination measure — the
            # pipeline reports it as a hallucinated *citation* and it is not one.
            if not exclude_noise and r["truth"] == "not_a_citation":
                tpos = False
            if opos and tpos:
                tp += 1
            elif opos and not tpos:
                fp += 1
                errors.append(r)
            elif not opos and tpos:
                fn += 1
                errors.append(r)
            else:
                tn += 1
        n = tp + fp + fn + tn
        prec_h = tp / (tp + fp) if tp + fp else None
        rec_h = tp / (tp + fn) if tp + fn else None
        prec_r = tn / (tn + fn) if tn + fn else None
        rec_r = tn / (tn + fp) if tn + fp else None
        return {"n": n, "tp": tp, "fp": fp, "fn": fn, "tn": tn,
                "hallucinated_precision": round(prec_h, 4) if prec_h is not None else None,
                "hallucinated_recall": round(rec_h, 4) if rec_h is not None else None,
                "real_precision": round(prec_r, 4) if prec_r is not None else None,
                "real_recall": round(rec_r, 4) if rec_r is not None else None,
                "accuracy": round((tp + tn) / n, 4) if n else None,
                "errors": [{k: e[k] for k in
                            ("i", "name", "arm", "task_id", "oracle", "truth", "note")}
                           for e in errors]}

    truth_counts = defaultdict(int)
    for r in rows:
        truth_counts[r["truth"]] += 1
    confusion = defaultdict(int)
    for r in rows:
        confusion[f"oracle={r['oracle']}|truth={r['truth']}"] += 1

    result = {
        "n_sample": len(rows),
        "truth_label_counts": dict(truth_counts),
        "confusion_cells": dict(sorted(confusion.items())),
        "strict_noise_as_FP": stats(exclude_noise=False),
        "lenient_noise_excluded": stats(exclude_noise=True),
        "all_rows": rows,
    }
    merge_json({"blinded_validation": result})
    r0 = result["strict_noise_as_FP"]
    print(json.dumps({k: v for k, v in result.items() if k != "all_rows"}, indent=1))
    print(f"\nstrict: precision(halluc)={r0['hallucinated_precision']} "
          f"recall(halluc)={r0['hallucinated_recall']} accuracy={r0['accuracy']}")


# --------------------------------------------------------------------------- #
# Sensitivity: adjusted oracle repairing the five FP modes the blinded        #
# validation exposed. All five rules are mechanical (no human judgment):      #
#   R1 comment prose      — every occurrence of the token sits after `--` or  #
#                           inside a doc/block comment line                   #
#   R2 import lines       — token starts with `Mathlib.` and only occurs on   #
#                           `import` lines (module name, not a decl)          #
#   R3 self-declaration   — the output itself declares the token              #
#                           (`theorem <token> ...` etc.)                      #
#   R4 dot-notation var   — first dotted segment is a single uppercase letter #
#                           (`M.det`) => projection on a variable, not a name #
#   R5 namespace short name — some single-segment prefix NS makes NS.<token>  #
#                           an indexed decl (the `open NS` convention)        #
# R1-R4 drop the token from the citation set; R5 reclassifies to exists.      #
# --------------------------------------------------------------------------- #
_DECL_KW_RE = re.compile(
    r"^\s*(?:@\[[^\]]*\]\s*)*(?:private\s+|protected\s+|noncomputable\s+|scoped\s+)*"
    r"(?:theorem|lemma|def|abbrev|structure|class|inductive|instance|opaque|axiom|example)\s+"
    r"([A-Za-z_][A-Za-z0-9_.']*)")


def build_suffix_openable(oracle: Oracle) -> set[str]:
    """names N such that some single-segment prefix NS gives NS.N in the index."""
    out: set[str] = set()
    for d in oracle.decls:
        head, dot, rest = d.partition(".")
        if dot and rest:
            out.add(rest)
    return out


def classify_adjusted(lean: str, name: str, oracle: Oracle,
                      openable: set[str]) -> str:
    """'noise' | 'exists' | 'renamed' | 'hallucinated' for one token."""
    base = oracle.classify(name)
    if base in ("exists", "renamed"):
        return base
    lines = (lean or "").splitlines()
    occ = [ln for ln in lines if name in ln]
    # R1: all occurrences after `--` or on doc/block-comment lines
    def commenty(ln: str) -> bool:
        idx = ln.find(name)
        cut = ln.find("--")
        return (0 <= cut < idx) or ln.lstrip().startswith(("/-", "/--", "*", "-/"))
    if occ and all(commenty(ln) for ln in occ):
        return "noise"
    # R2: module name on import lines only
    if name.startswith("Mathlib.") and occ and all(
            ln.lstrip().startswith("import") for ln in occ):
        return "noise"
    # R3: the output declares this very name
    for ln in lines:
        m = _DECL_KW_RE.match(ln)
        if m and m.group(1) == name:
            return "noise"
    # R4: dot-notation on a single-uppercase-letter variable
    head = name.split(".", 1)[0]
    if "." in name and len(head) == 1:
        return "noise"
    # R5: resolvable under one standard `open NS`
    if name in openable:
        return "exists"
    return "hallucinated"


def cmd_adjusted(_args) -> None:
    oracle = Oracle()
    assert oracle.enabled, "oracle sources missing"
    openable = build_suffix_openable(oracle)
    rows = load_fresh_rows()

    per_arm = {}
    run_flags: dict[str, dict[str, bool]] = {a: {} for a in ARMS}
    for arm in ARMS:
        cited = halluc = dropped = reclassified = 0
        for tid, r in rows[arm].items():
            lean = r.get("output_lean")
            any_h = False
            for n in extract_cited(lean):
                v0 = oracle.classify(n)
                v = classify_adjusted(lean, n, oracle, openable)
                if v == "noise":
                    dropped += 1
                    continue
                cited += 1
                if v0 == "hallucinated" and v == "exists":
                    reclassified += 1
                if v == "hallucinated":
                    halluc += 1
                    any_h = True
            run_flags[arm][tid] = any_h
        k = sum(run_flags[arm].values())
        n = len(run_flags[arm])
        per_arm[arm] = {
            "n_runs": n, "runs_any_halluc": k, "run_rate": round(k / n, 4),
            "run_wilson95": wilson95(k, n),
            "citations_kept": cited, "citations_halluc": halluc,
            "citation_rate": round(halluc / cited, 4) if cited else None,
            "tokens_dropped_R1_R4": dropped,
            "tokens_reclassified_R5": reclassified,
        }

    pairs = {}
    for x, y in [("D", "E"), ("D", "C"), ("D", "A"), ("D", "B"), ("E", "C")]:
        tids = sorted(set(run_flags[x]) & set(run_flags[y]))
        b = sum(1 for t in tids if run_flags[x][t] and not run_flags[y][t])
        c = sum(1 for t in tids if not run_flags[x][t] and run_flags[y][t])
        pairs[f"{x}_vs_{y}"] = {
            "n_paired": len(tids), f"{x}_only": b, f"{y}_only": c,
            "discordant": b + c,
            "p_mcnemar_exact_two_sided": mcnemar_exact(b, c),
        }

    result = {"rules": "R1 comments, R2 imports, R3 self-declarations, "
                       "R4 single-letter dot-notation heads (dropped); "
                       "R5 single-segment-prefix namespace resolution "
                       "(reclassified to exists)",
              "per_arm": per_arm, "mcnemar_pairs": pairs}
    merge_json({"adjusted_oracle_sensitivity": result})
    print(json.dumps(result, indent=1))


# --------------------------------------------------------------------------- #
# plumbing                                                                    #
# --------------------------------------------------------------------------- #
def provenance_block() -> dict:
    import hashlib
    decl_data = (REPO / ".claude" / "skills" / "mathlib-search" / ".cache"
                 / "declaration-data.json")
    etag_p = decl_data.with_suffix(".etag")
    return {
        "fresh_rows": "bench/data/runs/{A..E}/fresh_*.json (100/arm, post-repair; "
                      "31 arm-E rows rerun 2026-07-27 per bridge_summary_v2.json "
                      "v2_provenance)",
        "classifier": "bench/score_bridge.py extract_cited + Oracle (imported)",
        "oracle_decl_index": str(decl_data.relative_to(REPO)),
        "oracle_decl_index_etag": etag_p.read_text().strip() if etag_p.exists() else None,
        "oracle_rename_map": "catalog/data/decl_renames.jsonl",
        "ground_truth_tree": f"mathlib4 @ {PIN_AGENT} (agents' pinned tree), "
                             f"cross-check {PIN_TYPECHECK} (bench-lean-fresh rev)",
        "seed": DEFAULT_SEED,
    }


def merge_json(update: dict) -> None:
    cur = json.loads(OUT_JSON.read_text()) if OUT_JSON.exists() else {}
    cur.update(update)
    OUT_JSON.write_text(json.dumps(cur, indent=1))
    print(f"merged -> {OUT_JSON}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("table")
    sp = sub.add_parser("sample")
    sp.add_argument("--seed", type=int, default=DEFAULT_SEED)
    sub.add_parser("evidence")
    sub.add_parser("compare")
    sub.add_parser("adjusted")
    args = ap.parse_args()
    {"table": cmd_table, "sample": cmd_sample, "evidence": cmd_evidence,
     "compare": cmd_compare, "adjusted": cmd_adjusted}[args.cmd](args)
    return 0


if __name__ == "__main__":
    sys.exit(main())
