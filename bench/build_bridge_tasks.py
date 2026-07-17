#!/usr/bin/env python3
"""Build bench/data/bridge_tasks.jsonl for the Bridge Experiment (Tier 1a).

Source: ProofNet# (PAug/ProofNetSharp) — a corrected Lean 4 port of ProofNet,
MIT-licensed, 371 undergraduate-textbook problems, each with an informal NL
statement, an informal NL proof, a Lean 4 statement, and a src header. Introduced
in "Improving Autoformalization using Type Checking" (Poiroux et al., arXiv:2406.07222).

Design constraints (docs/research/BRIDGE-EXPERIMENT.md, Tier 1a):
  * gold_formal = the Lean 4 STATEMENT only, ending ':= sorry' (proofs stripped —
    ProofNet# ships statements ending in ':=', so we just append ' sorry').
  * informal_statement carries the NL statement ONLY. The NL proof is NEVER shown
    to agents (would contaminate the treatment miniF2F-style); it is quarantined in
    'informal_proof_gold' and flagged in the _meta line.
  * splits: deterministic seed 20260716, 30-problem dev split stratified by domain
    (largest-remainder allocation), rest eval. dev is for prompt iteration ONLY.

Reproducible: rows sorted by id; regeneration is byte-identical given the same
raw parquet snapshot under bench/data/raw/proofnetsharp/.

Run:  uv run --with pyarrow python3 bench/build_bridge_tasks.py
"""
from __future__ import annotations
import json, math, random, hashlib, datetime, pathlib, sys

HERE = pathlib.Path(__file__).resolve().parent
RAW = HERE / "data" / "raw" / "proofnetsharp"
OUT = HERE / "data" / "bridge_tasks.jsonl"
STATS = HERE / "data" / "bridge_tasks.stats.json"

SEED = 20260716
DEV_N = 30
SOURCE_TAG = "proofnet-lean4"
SOURCE_REPO = "PAug/ProofNetSharp"
SOURCE_URL = "https://huggingface.co/datasets/PAug/ProofNetSharp"
SOURCE_REVISION = "a8da405fbd1e348a87445c2e562c747b7e26dc8f"  # sha as of 2025-03-24
SOURCE_LICENSE = "MIT"
PAPER = "arXiv:2406.07222 (Poiroux et al., Improving Autoformalization using Type Checking)"
GEN_DATE = "2026-07-16"


def load_rows():
    import pyarrow.parquet as pq
    rows = []
    for fn, orig in [("valid-00000-of-00001.parquet", "valid"),
                     ("test-00000-of-00001.parquet", "test")]:
        for r in pq.read_table(RAW / fn).to_pylist():
            r["_orig_split"] = orig
            rows.append(r)
    return rows


def largest_remainder(counts: dict[str, int], total_dev: int) -> dict[str, int]:
    """Stratified allocation of total_dev across domains, proportional to counts,
    summing exactly to total_dev, with a deterministic tie-break (by domain name)."""
    N = sum(counts.values())
    quota = {d: c * total_dev / N for d, c in counts.items()}
    base = {d: int(math.floor(q)) for d, q in quota.items()}
    remaining = total_dev - sum(base.values())
    # rank by fractional remainder desc, tie-break by domain name asc (deterministic)
    order = sorted(counts, key=lambda d: (-(quota[d] - base[d]), d))
    for d in order[:remaining]:
        base[d] += 1
    return base


def main():
    rows = load_rows()
    for r in rows:
        assert "|" in r["id"], f"unexpected id form: {r['id']}"
        assert r["nl_statement"].strip(), f"empty nl_statement: {r['id']}"
        lf = r["lean4_formalization"].rstrip()
        assert lf.endswith(":="), f"formalization not statement-only: {r['id']}"

    # domain = book prefix
    domains = {}
    for r in rows:
        domains.setdefault(r["id"].split("|", 1)[0], []).append(r)
    dom_counts = {d: len(v) for d, v in domains.items()}

    dev_alloc = largest_remainder(dom_counts, DEV_N)
    assert sum(dev_alloc.values()) == DEV_N, dev_alloc

    # deterministic stratified sampling: process domains in sorted order, seeded once
    rng = random.Random(SEED)
    dev_ids: set[str] = set()
    for d in sorted(domains):
        ids = sorted(r["id"] for r in domains[d])
        k = dev_alloc[d]
        dev_ids.update(rng.sample(ids, k))

    tasks = []
    for r in rows:
        rid = r["id"]
        gold = r["lean4_formalization"].rstrip() + " sorry"
        tasks.append({
            "id": rid,
            "source": SOURCE_TAG,
            "informal_statement": r["nl_statement"].strip(),
            "informal_context": "",  # ProofNet# ships no non-proof context; proof is quarantined
            "gold_formal": gold,
            "gold_header": r["lean4_src_header"],
            "split": "dev" if rid in dev_ids else "eval",
            "domain": rid.split("|", 1)[0],
            # NEVER shown to agents — reference-only gold for grading/analysis:
            "informal_proof_gold": (r.get("nl_proof") or "").strip(),
            "orig_split": r["_orig_split"],
        })
    tasks.sort(key=lambda t: t["id"])

    meta = {
        "_meta": {
            "note": ("dev split is for PROMPT ITERATION ONLY — never tune on or "
                     "report eval-tuned numbers. 'informal_proof_gold' is the gold "
                     "NL proof and is NEVER shown to agents (reference-only for "
                     "grading/analysis); showing it would contaminate the treatment."),
            "experiment": "Bridge Experiment Tier 1a (docs/research/BRIDGE-EXPERIMENT.md)",
            "task": "statement autoformalization: informal_statement -> gold_formal (Lean 4, ':= sorry')",
            "source": SOURCE_TAG,
            "source_repo": SOURCE_REPO,
            "source_url": SOURCE_URL,
            "source_revision": SOURCE_REVISION,
            "source_license": SOURCE_LICENSE,
            "source_paper": PAPER,
            "lean_compat": "Lean v4.7.0 .. v4.16.0-rc2 (per dataset card)",
            "seed": SEED,
            "dev_n": DEV_N,
            "generated": GEN_DATE,
            "row_count": len(tasks),
        }
    }

    with OUT.open("w") as f:
        f.write(json.dumps(meta, ensure_ascii=False) + "\n")
        for t in tasks:
            f.write(json.dumps(t, ensure_ascii=False) + "\n")

    # stats
    from collections import Counter
    per_dom = Counter(t["domain"] for t in tasks)
    per_dom_dev = Counter(t["domain"] for t in tasks if t["split"] == "dev")
    per_dom_eval = Counter(t["domain"] for t in tasks if t["split"] == "eval")
    payload = json.dumps([{k: v for k, v in t.items()} for t in tasks],
                         ensure_ascii=False, sort_keys=True).encode()
    stats = {
        "source": SOURCE_TAG,
        "source_repo": SOURCE_REPO,
        "source_url": SOURCE_URL,
        "source_revision": SOURCE_REVISION,
        "source_license": SOURCE_LICENSE,
        "source_paper": PAPER,
        "lean_compat": "Lean v4.7.0 .. v4.16.0-rc2",
        "generated": GEN_DATE,
        "seed": SEED,
        "row_count": len(tasks),
        "split_counts": {"dev": DEV_N, "eval": len(tasks) - DEV_N},
        "domains": sorted(per_dom),
        "per_domain": {d: {"total": per_dom[d], "dev": per_dom_dev[d], "eval": per_dom_eval[d]}
                       for d in sorted(per_dom)},
        "rows_missing_informal_proof_gold": sorted(
            t["id"] for t in tasks if not t["informal_proof_gold"]),
        "content_sha256": hashlib.sha256(payload).hexdigest(),
    }
    with STATS.open("w") as f:
        json.dump(stats, f, indent=2, ensure_ascii=False)
        f.write("\n")

    print(f"wrote {OUT} ({len(tasks)} tasks + 1 _meta line)")
    print(f"wrote {STATS}")
    print(f"dev allocation: {dict(sorted(per_dom_dev.items()))} = {sum(per_dom_dev.values())}")
    print(f"content_sha256: {stats['content_sha256']}")


if __name__ == "__main__":
    main()
