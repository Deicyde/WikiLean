#!/usr/bin/env python3
"""Decl-existence sweep — verify every cited Mathlib decl and emit a REVIEW proposal.

WikiLean's one core promise is that a "formalized" badge points at a real Mathlib
declaration. This sweep checks every `mathlib.decl` cited in the annotation corpus
against a COMPLETE oracle and categorizes the misses for human review. It writes a
proposal file only — it NEVER mutates D1 (D1 is canonical; disk is cache/backup).
Applying any fix is a separate, human-approved step through the Worker API.

Why a UNION oracle (this is the whole point):
  The doc-gen4 cache `.claude/skills/mathlib-search/.cache/declaration-data.json`
  is large (~415k names) but demonstrably INCOMPLETE — it is missing known-real
  decls like `Basis`, `Basis.dualBasis`, and ~all `Basis.*` lemmas. Trusting it
  alone reports ~5.7% "missing", mostly FALSE POSITIVES. So we union it with
  fully-qualified names parsed straight from the local mathlib checkout (which has
  the human-written decls the cache lacks; the cache in turn has the auto-generated
  decls — `.mk`/`.rec`/projections — that source-parsing lacks). A decl absent from
  BOTH is a genuine review candidate.

Categories for each miss (cited decl in NEITHER oracle):
  proof_wanted_downgrade : the decl is a Mathlib `proof_wanted` stub (declared but
                           UNPROVEN) — the annotation over-claims. Downgrade
                           formalized/partial -> not_formalized. (Reputationally the
                           most urgent: this is why the DB says "Poincare is
                           formalized in Mathlib".)
  likely_rename          : exactly one oracle name shares the decl's last segment
                           (near-certain namespace drift, e.g. goldenRatio ->
                           Real.goldenRatio). Suggested rewrite, still human-gated.
  ambiguous_rename       : several last-segment matches — needs disambiguation.
  no_match               : nothing shares the last segment — strongest
                           hallucination / removed-decl candidate.

Usage:
  python3 manage/decl_existence_sweep.py [--mathlib DIR] [--out FILE] [--quiet]
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from collections import Counter, defaultdict
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
ANNOT_DIR = REPO / "site" / "annotations"
CACHE_ORACLE = REPO / ".claude" / "skills" / "mathlib-search" / ".cache" / "declaration-data.json"
DEFAULT_MATHLIB = Path(os.environ.get("WIKILEAN_MATHLIB", "/Users/jack/Desktop/LEAN/mathlib4"))
DEFAULT_OUT = REPO / "manage" / "data" / "decl_sweep_proposal.json"

# Lean 4 declaration keywords whose following identifier is a decl name.
_DECL_RE = re.compile(
    r"^\s*(?:@\[[^\]]*\]\s*)*"
    r"(?:noncomputable\s+|private\s+|protected\s+|scoped\s+|local\s+|"
    r"partial\s+|unsafe\s+|nonrec\s+|mutual\s+)*"
    r"(theorem|lemma|def|abbrev|structure|inductive|class|opaque|axiom)\s+"
    r"([A-Za-z_][A-Za-z0-9_'.À-￿]*)"
)
# `proof_wanted NAME` — a declared-but-unproven Mathlib statement.
_PROOF_WANTED_RE = re.compile(r"\bproof_wanted\s+([A-Za-z_][A-Za-z0-9_'.À-￿]*)")
_NAMESPACE_RE = re.compile(r"^\s*namespace\s+([A-Za-z_][A-Za-z0-9_'.À-￿]*)")
_SECTION_RE = re.compile(r"^\s*section\b")
_END_RE = re.compile(r"^\s*end\b")


# ---------------------------------------------------------------------------
# Oracle assembly
# ---------------------------------------------------------------------------
def load_cache_oracle() -> set[str]:
    if not CACHE_ORACLE.exists():
        print(f"warning: cache oracle absent ({CACHE_ORACLE})", file=sys.stderr)
        return set()
    d = json.loads(CACHE_ORACLE.read_text())
    decls = d.get("declarations", {})
    return set(decls.keys()) if isinstance(decls, dict) else set(decls)


def parse_source_names(mathlib_dir: Path) -> tuple[set[str], set[str]]:
    """Scan the checkout for fully-qualified decl names and proof_wanted stubs.

    Returns (decl_names, proof_wanted_names). Namespace-stack aware: `namespace X`
    and `section` share one stack popped on `end`; only namespaces contribute to
    the fully-qualified prefix (Lean semantics)."""
    names: set[str] = set()
    proof_wanted: set[str] = set()
    root = mathlib_dir / "Mathlib"
    if not root.exists():
        print(f"warning: mathlib checkout absent ({root}) — cache-only oracle",
              file=sys.stderr)
        return names, proof_wanted
    files = list(root.rglob("*.lean"))
    for fp in files:
        stack: list[str | None] = []  # str = namespace name, None = section
        try:
            text = fp.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for line in text.splitlines():
            m = _NAMESPACE_RE.match(line)
            if m:
                stack.append(m.group(1))
                continue
            if _SECTION_RE.match(line):
                stack.append(None)
                continue
            if _END_RE.match(line):
                if stack:
                    stack.pop()
                continue
            pw = _PROOF_WANTED_RE.search(line)
            if pw:
                prefix = ".".join(s for s in stack if s)
                nm = pw.group(1)
                proof_wanted.add(f"{prefix}.{nm}" if prefix else nm)
                # proof_wanted also DECLARES the name, so it exists as a decl too.
                names.add(f"{prefix}.{nm}" if prefix else nm)
            dm = _DECL_RE.match(line)
            if dm:
                nm = dm.group(2)
                prefix = ".".join(s for s in stack if s)
                names.add(f"{prefix}.{nm}" if prefix else nm)
    return names, proof_wanted


# ---------------------------------------------------------------------------
# Corpus scan
# ---------------------------------------------------------------------------
def scan_corpus() -> tuple[dict, Counter]:
    """Returns (by_decl, match_kind_counter). by_decl[decl] = list of citation
    records {slug, id, status, label, note, match_kind, provenance}."""
    by_decl: dict[str, list[dict]] = defaultdict(list)
    mk_counter: Counter = Counter()
    for f in sorted(ANNOT_DIR.glob("*.json")):
        if f.name.endswith(".agent1.json"):
            continue
        try:
            d = json.loads(f.read_text())
        except (json.JSONDecodeError, OSError):
            continue
        slug = d.get("slug") or f.stem
        for a in d.get("annotations") or []:
            if not isinstance(a, dict):
                continue
            ml = a.get("mathlib") or {}
            decl = ml.get("decl") or a.get("decl")
            if not decl:
                continue
            by_decl[decl].append({
                "slug": slug,
                "id": a.get("id"),
                "status": a.get("status"),
                "label": a.get("label"),
                "note": (a.get("note") or "")[:240],
                "match_kind": ml.get("match_kind"),
                "provenance": a.get("provenance"),
            })
            mk_counter[ml.get("match_kind")] += 1
    return by_decl, mk_counter


def classify(decl: str, oracle: set[str], proof_wanted: set[str],
             last_seg_index: dict[str, list[str]]) -> tuple[str, list[str]]:
    if decl in proof_wanted:
        return "proof_wanted_downgrade", []
    last = decl.split(".")[-1]
    candidates = [c for c in last_seg_index.get(last, []) if c != decl]
    if len(candidates) == 1:
        return "likely_rename", candidates
    if len(candidates) > 1:
        # cap the candidate list so the proposal stays readable
        return "ambiguous_rename", sorted(candidates)[:12]
    return "no_match", []


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--mathlib", type=Path, default=DEFAULT_MATHLIB,
                    help="mathlib4 checkout (for the source oracle)")
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT)
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args()

    t0 = time.time()
    cache = load_cache_oracle()
    src_names, proof_wanted = parse_source_names(args.mathlib)
    oracle = cache | src_names
    # last-segment index over the union, for rename suggestions.
    last_seg_index: dict[str, list[str]] = defaultdict(list)
    for name in oracle:
        last_seg_index[name.split(".")[-1]].append(name)

    by_decl, mk_counter = scan_corpus()
    total_decls = len(by_decl)
    total_citations = sum(len(v) for v in by_decl.values())

    misses = {d: recs for d, recs in by_decl.items() if d not in oracle}
    cats: dict[str, list[dict]] = defaultdict(list)
    for decl, recs in sorted(misses.items()):
        cat, suggested = classify(decl, oracle, proof_wanted, last_seg_index)
        articles = sorted({r["slug"] for r in recs})
        statuses = Counter(r["status"] for r in recs)
        cats[cat].append({
            "decl": decl,
            "citation_count": len(recs),
            "articles": articles,
            "status_dist": dict(statuses),
            "match_kinds": dict(Counter(r["match_kind"] for r in recs)),
            "suggested": suggested,
            "annotations": recs,
        })

    # proof_wanted overclaims — an INDEPENDENT pass over the whole corpus, not the
    # miss path: a proof_wanted decl DOES exist (it is declared), so it is never an
    # existence miss. The defect is the STATUS — a declared-but-unproven stub badged
    # formalized/partial claims Mathlib has proved something it has not (this is why
    # the DB says "Poincare is formalized"). Downgrade those to not_formalized.
    overclaims = []
    proof_wanted_cited = []  # informational: every proof_wanted decl the corpus cites
    for decl, recs in sorted(by_decl.items()):
        if decl not in proof_wanted:
            continue
        proof_wanted_cited.append({
            "decl": decl, "citation_count": len(recs),
            "status_dist": dict(Counter(r["status"] for r in recs)),
            "articles": sorted({r["slug"] for r in recs}),
        })
        for r in recs:
            if r["status"] in ("formalized", "partial"):
                overclaims.append({
                    "slug": r["slug"], "id": r["id"], "decl": decl,
                    "current_status": r["status"], "proposed_status": "not_formalized",
                    "label": r["label"],
                })

    n_miss_decls = len(misses)
    n_miss_cit = sum(len(v) for v in misses.values())
    proposal = {
        "generated_at": int(t0),
        "generated_at_iso": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(t0)),
        "note": ("REVIEW PROPOSAL — not applied. D1 is canonical; disk lags. Apply "
                 "approved changes via the Worker API (POST /api/article/:slug with "
                 "base_version), keyed by (slug, annotation id). Suggestions are "
                 "human-gated: a unique last-segment match is a hint, not a proof."),
        "oracle": {
            "cache_names": len(cache),
            "source_names": len(src_names),
            "union_names": len(oracle),
            "proof_wanted_stubs": len(proof_wanted),
            "mathlib_dir": str(args.mathlib),
        },
        "corpus": {
            "distinct_cited_decls": total_decls,
            "total_citations": total_citations,
            "match_kind_dist": {str(k): v for k, v in mk_counter.items()},
        },
        "misses": {
            "distinct_decls": n_miss_decls,
            "pct_distinct": round(100 * n_miss_decls / total_decls, 2) if total_decls else 0,
            "citations": n_miss_cit,
            "pct_citations": round(100 * n_miss_cit / total_citations, 2) if total_citations else 0,
            "by_category": {c: len(v) for c, v in cats.items()},
        },
        "overclaims_proof_wanted_badged_formalized": overclaims,
        "proof_wanted_cited": proof_wanted_cited,
        "categories": cats,
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    tmp = args.out.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(proposal, ensure_ascii=False, indent=1))
    tmp.replace(args.out)

    if not args.quiet:
        print(f"decl-existence sweep  ({time.time() - t0:.1f}s)")
        print(f"  oracle: {len(cache):,} cache + {len(src_names):,} source "
              f"= {len(oracle):,} union names  ({len(proof_wanted)} proof_wanted stubs)")
        print(f"  corpus: {total_citations:,} citations / {total_decls:,} distinct decls")
        print(f"  MISSES: {n_miss_decls} distinct ({proposal['misses']['pct_distinct']}%) "
              f"= {n_miss_cit} citations ({proposal['misses']['pct_citations']}%)")
        for c in ("likely_rename", "ambiguous_rename", "no_match"):
            items = cats.get(c, [])
            cit = sum(i["citation_count"] for i in items)
            print(f"    {c:24s} {len(items):4d} decls / {cit:4d} citations")
        print(f"  proof_wanted decls cited: {len(proof_wanted_cited)}")
        print(f"  OVERCLAIMS (proof_wanted badged formalized/partial): {len(overclaims)}")
        for o in overclaims:
            print(f"    - /{o['slug']}  [{o['current_status']}] {o['decl']}")
        print(f"  -> {args.out.relative_to(REPO)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
