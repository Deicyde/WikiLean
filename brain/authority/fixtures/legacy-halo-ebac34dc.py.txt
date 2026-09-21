#!/usr/bin/env python3
"""Mathlib-halo gap analysis — rank the ring-1 frontier of the Brain.

A *ring-1 halo* cell is a Brain cell that at least one external math database
writes about (>=1 `page` organ) but Mathlib has no declaration for (0 `decl`
organs): the informal literature considers it a named object of mathematics,
the formal library does not. This script extracts that ring, scores each cell
by how formalized its neighborhood is (a cell ALL of whose neighbors are
formalized is the natural "next brick" — the frontier), ranks the list, and
cross-checks the top of it against (a) the decl-existence UNION oracle and
(b) LeanSearch, to separate genuine Mathlib gaps from already-formalized-but-
untagged concepts.

Everything is re-derived at runtime from brain/data/{cells,synapses}.jsonl —
no count in here is hardcoded, so the script tracks the nightly Brain builds.

Method:
  neighbor fractions   For each ring-1 cell, over its cell<->cell synapses:
                       depends-frac uses only synapses whose kinds histogram
                       has depends>0 (Mathlib decl-dependency traces, the
                       strongest signal); all-frac uses every bond kind. A
                       neighbor counts as formalized iff it has >=1 decl organ.
  ranking              primary = depends-frac desc; cells with no depends bond
                       fall back to all-frac (flagged via frac_source) rather
                       than sinking below depends-frac=0 cells — a cell bonded
                       only by mentions/links but with a fully-formalized
                       neighborhood is still frontier. Tiebreak = centrality
                       (PageRank from manage/data/centrality.json, joined by
                       QID; missing QID -> 0), then label for determinism.
  cross-check (top N)  Verdicts are deliberately conservative:
                       false_positive     — a plausible name form of the label
                                            EXISTS in the union oracle (exact /
                                            last-segment, case-insensitive).
                                            Existence-verified; decl named.
                                            Lexical: a human still confirms the
                                            decl means THIS concept.
                       possibly_tagged_gap — no verified name, but fuzzy oracle
                                            matches or on-topic LeanSearch hits
                                            exist; needs human review.
                       confirmed_gap      — both probes came back empty:
                                            strongest genuine-gap candidate.

Usage:
  python3 manage/halo.py [--top 50] [--offline] [--cache-oracle-only]
                         [--out FILE] [--md-out FILE] [--quiet]

Writes manage/data/halo.json (full ranked ring + metadata) and
docs/research/HALO.md (dated report; prose template + data-generated tables).
Network use: LeanSearch only (batched, throttled); --offline skips it.
"""
from __future__ import annotations

import argparse
import json
import re
import ssl
import sys
import time
import unicodedata
import urllib.request
from collections import defaultdict
from itertools import permutations, product
from pathlib import Path

# macOS framework Pythons ship without a CA bundle wired into ssl; prefer
# certifi when present so the LeanSearch call verifies instead of failing.
try:
    import certifi
    _SSL_CTX = ssl.create_default_context(cafile=certifi.where())
except ImportError:  # pragma: no cover — system trust store
    _SSL_CTX = ssl.create_default_context()

REPO = Path(__file__).resolve().parent.parent
HERE = Path(__file__).resolve().parent
DATA = HERE / "data"
CELLS = REPO / "brain" / "data" / "cells.jsonl"
SYNAPSES = REPO / "brain" / "data" / "synapses.jsonl"
CENTRALITY = DATA / "centrality.json"
DEFAULT_OUT = DATA / "halo.json"
DEFAULT_MD = REPO / "docs" / "research" / "HALO.md"

sys.path.insert(0, str(HERE))
import decl_existence_sweep as sweep  # union-oracle assembly (cache + source)

LEANSEARCH_URL = "https://leansearch.net/search"
LEANSEARCH_BATCH = 10       # labels per POST (the API takes a query list)
LEANSEARCH_SLEEP = 2.5      # seconds between batches — well under the rate cap
LEANSEARCH_RESULTS = 6

# These DBs anchor to the Brain exclusively through decl attributes/references
# (@[stacks], @[kerodon], formal-conjectures URLs), so every cell carrying one
# of their page organs has a decl BY CONSTRUCTION — their ring-1 count is a
# structural zero, not evidence of full coverage. Documented, not hidden.
STRUCTURAL_ZERO_DBS = ("stacks", "kerodon", "erdos")

_QID_RE = re.compile(r"^Q\d+$")
_STOP = {"the", "of", "a", "an", "in", "for", "and"}


# ---------------------------------------------------------------------------
# Brain load
# ---------------------------------------------------------------------------
def _jsonl(path: Path):
    for line in path.read_text().splitlines():
        if line.strip():
            yield json.loads(line)


def load_brain() -> tuple[dict, str]:
    """cells keyed by id (meta row stripped), plus the build timestamp."""
    cells: dict[str, dict] = {}
    build = "unknown"
    for r in _jsonl(CELLS):
        if "_meta" in r:
            build = r["_meta"].get("generated_at", build)
            continue
        cells[r["id"]] = r
    return cells, build


def load_adjacency() -> tuple[dict, dict, int]:
    """(all_nb, dep_nb, n_cellcell) over cell<->cell synapses only.

    Synapses are undirected aggregates; dep_nb keeps only bonds whose kinds
    histogram carries at least one depends trace."""
    all_nb: dict[str, set] = defaultdict(set)
    dep_nb: dict[str, set] = defaultdict(set)
    n = 0
    for r in _jsonl(SYNAPSES):
        if "_meta" in r:
            continue
        s, d = r.get("src", ""), r.get("dst", "")
        if not (s.startswith("cell:") and d.startswith("cell:")):
            continue  # cell<->path / path<->path containment bonds are not neighbors
        n += 1
        all_nb[s].add(d)
        all_nb[d].add(s)
        if r.get("kinds", {}).get("depends", 0) > 0:
            dep_nb[s].add(d)
            dep_nb[d].add(s)
    return all_nb, dep_nb, n


# ---------------------------------------------------------------------------
# Ring extraction + ranking
# ---------------------------------------------------------------------------
def cell_qid(r: dict) -> str | None:
    if _QID_RE.match(r.get("anchor", "")):
        return r["anchor"]
    for o in r.get("organs", []):
        if o.get("kind") == "concept" and _QID_RE.match(o.get("id", "")):
            return o["id"]
    return None


def build_ring(cells: dict, all_nb: dict, dep_nb: dict, cen_scores: dict) -> list[dict]:
    formalized = {cid for cid, r in cells.items()
                  if any(o.get("kind") == "decl" for o in r.get("organs", []))}

    def frac(nbs: set) -> tuple[int, int, float | None]:
        if not nbs:
            return 0, 0, None
        f = sum(1 for x in nbs if x in formalized)
        return len(nbs), f, round(f / len(nbs), 4)

    rows = []
    for cid, r in cells.items():
        if cid in formalized:
            continue
        dbs = sorted({o["db"] for o in r.get("organs", []) if o.get("kind") == "page"})
        if not dbs:
            continue  # ring-1 requires an external page organ
        dn, dform, dfrac = frac(dep_nb.get(cid, set()))
        an, aform, afrac = frac(all_nb.get(cid, set()))
        qid = cell_qid(r)
        c = cen_scores.get(qid, {}) if qid else {}
        rows.append({
            "cell": cid, "qid": qid, "label": r.get("label", cid),
            "dbs": dbs,
            "depends_n": dn, "depends_formalized": dform, "depends_frac": dfrac,
            "all_n": an, "all_formalized": aform, "all_frac": afrac,
            "frac_source": "depends" if dfrac is not None else
                           ("all" if afrac is not None else None),
            "isolated": an == 0,
            "pagerank": c.get("pagerank", 0.0),
            "centrality_pct": c.get("centrality_pct", 0.0),
        })

    def key(row):
        primary = row["depends_frac"] if row["depends_frac"] is not None else (
            row["all_frac"] if row["all_frac"] is not None else -1.0)
        return (-primary, -row["pagerank"], row["label"], row["cell"])

    rows.sort(key=key)
    for i, row in enumerate(rows, 1):
        row["rank"] = i
    return rows


# ---------------------------------------------------------------------------
# Cross-check: union oracle + LeanSearch
# ---------------------------------------------------------------------------
def _fold(s: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFKD", s)
                   if not unicodedata.combining(c))


def _deplural(t: str) -> str | None:
    if t.endswith("s") and not t.endswith("ss") and len(t) > 3:
        return t[:-1]
    return None


def label_keys(label: str) -> tuple[list[str], list[str], list[str], str]:
    """(match_keys, perm_keys, main_tokens, search_query) for a cell label.

    match_keys are lowercase concatenations checked against oracle last
    segments; perm_keys are every token-order permutation (with depluralized
    variants), matched against WHOLE flattened names so `Polynomial.laguerre`
    answers for "Laguerre polynomial". main_tokens drop the parenthetical
    disambiguator; the query keeps it."""
    main = re.sub(r"\([^)]*\)", " ", label)
    toks_main = [t.lower() for t in re.findall(r"[A-Za-z0-9]+", _fold(main))
                 if t.lower() not in _STOP]
    toks_all = [t.lower() for t in re.findall(r"[A-Za-z0-9]+", _fold(label))
                if t.lower() not in _STOP]
    keys: list[str] = []
    perm_keys: list[str] = []
    if toks_main:
        cat = "".join(toks_main)
        keys = [cat, "is" + cat]
        dp = _deplural(toks_main[-1])
        if dp:
            keys.append("".join(toks_main[:-1] + [dp]))
        if len(toks_main) <= 4:
            variants = [[t] + ([_deplural(t)] if _deplural(t) else [])
                        for t in toks_main]
            perm_keys = sorted({"".join(combo)
                                for order in permutations(range(len(toks_main)))
                                for combo in product(*(variants[i] for i in order))})
    return keys, perm_keys, toks_main, " ".join(toks_all)


def leansearch(queries: list[str], quiet: bool) -> tuple[dict, int]:
    """label query -> raw result list; returns (results, n_errors)."""
    out: dict[str, list] = {}
    errors = 0
    for i in range(0, len(queries), LEANSEARCH_BATCH):
        batch = queries[i:i + LEANSEARCH_BATCH]
        if i:
            time.sleep(LEANSEARCH_SLEEP)
        body = json.dumps({"query": batch, "num_results": LEANSEARCH_RESULTS}).encode()
        req = urllib.request.Request(
            LEANSEARCH_URL, data=body,
            headers={"Content-Type": "application/json",
                     "User-Agent": "WikiLean-halo/1.0 (wikilean.jackmccarthy.org)"})
        try:
            with urllib.request.urlopen(req, timeout=60, context=_SSL_CTX) as resp:
                data = json.loads(resp.read().decode())
            for q, hits in zip(batch, data):
                out[q] = hits or []
        except Exception as e:  # noqa: BLE001 — degrade to oracle-only, loudly
            errors += 1
            print(f"  ! leansearch batch failed ({e}) — {len(batch)} labels unchecked",
                  file=sys.stderr)
        if not quiet:
            print(f"  leansearch {min(i + LEANSEARCH_BATCH, len(queries))}/{len(queries)}",
                  file=sys.stderr)
    return out, errors


def crosscheck(rows: list[dict], top_n: int, offline: bool,
               cache_only: bool, mathlib_dir: Path, quiet: bool) -> dict:
    """Attach verdict/evidence to rows[:top_n] in place; return meta."""
    cache = sweep.load_cache_oracle()
    src_names: set[str] = set()
    if not cache_only:
        src_names, _pw = sweep.parse_source_names(mathlib_dir)
    oracle = cache | src_names
    last_seg: dict[str, list[str]] = defaultdict(list)
    flat_names: list[tuple[str, str]] = []  # (flattened lowercase, name)
    for name in oracle:
        last_seg[name.split(".")[-1].lower()].append(name)
        flat_names.append((name.lower().replace(".", "").replace("_", "")
                           .replace("'", ""), name))

    top = rows[:top_n]
    ls_results, ls_errors = ({}, 0)
    queries = []
    if not offline:
        queries = [label_keys(r["label"])[3] for r in top]
        ls_results, ls_errors = leansearch(queries, quiet)

    verdicts: dict[str, int] = defaultdict(int)
    for r in top:
        keys, perm_keys, toks_main, query = label_keys(r["label"])
        # tier 1 — existence-verified name forms: concatenated last segment, or
        # a whole name that is a permutation of the label's tokens.
        exact = sorted({n for k in keys for n in last_seg.get(k, [])})
        pk = set(perm_keys)
        exact += sorted(n for flat, n in flat_names if flat in pk and n not in exact)

        # tier 2a — last segments containing the full concatenation.
        fuzzy = []
        long_keys = [k for k in keys if len(k) >= 6]
        if not exact and long_keys:
            for seg, names in last_seg.items():
                if any(k in seg for k in long_keys):
                    fuzzy.extend(names)
            fuzzy = sorted(set(fuzzy))[:5]
        # tier 2b — names containing EVERY label token somewhere (catches
        # namespaced/reordered forms like Frullani.integral_Ioi_eq). Only when
        # every main token is >=4 chars, so a stray short token ("F-…") cannot
        # make one generic word attest the whole label.
        toks4 = [t for t in toks_main if len(t) >= 4]
        token_hits: list[str] = []
        if not exact and toks4 and toks4 == toks_main:
            token_hits = sorted(n for flat, n in flat_names
                                if all(t in flat for t in toks4))[:6]

        phrase = " ".join(toks_main)
        hits = []
        for h in ls_results.get(query, []):
            res = h.get("result", h)
            full = ".".join(res.get("name") or [])
            flat = full.lower().replace(".", "").replace("_", "").replace("'", "")
            blob = ((res.get("informal_name") or "") + " " +
                    (res.get("informal_description") or "")[:400]).lower()
            namehit = any(k in flat for k in long_keys) or (
                bool(toks4) and toks4 == toks_main and all(t in flat for t in toks4))
            phrasehit = len(toks_main) >= 2 and (
                phrase in blob or (phrase + "s") in blob)
            if namehit or phrasehit:
                hits.append({"decl": full,
                             "module": ".".join(res.get("module_name") or []),
                             "kind": res.get("kind"),
                             "informal_name": res.get("informal_name")})
        hits = hits[:4]

        if exact:
            r["verdict"] = "false_positive"
            r["existing_decl"] = min(exact, key=len)
        elif fuzzy or token_hits or hits:
            r["verdict"] = "possibly_tagged_gap"
            r["existing_decl"] = None
        else:
            r["verdict"] = "confirmed_gap"
            r["existing_decl"] = None
        r["evidence"] = {"oracle_exact": exact[:8], "oracle_fuzzy": fuzzy,
                         "oracle_token": token_hits, "leansearch": hits}
        verdicts[r["verdict"]] += 1

    return {
        "top_n": len(top),
        "oracle": {"cache_names": len(cache), "source_names": len(src_names),
                   "union_names": len(oracle), "cache_only": cache_only,
                   "mathlib_dir": str(mathlib_dir)},
        "leansearch": {"endpoint": LEANSEARCH_URL, "skipped": offline,
                       "labels_queried": len(queries), "batch_errors": ls_errors,
                       "num_results": LEANSEARCH_RESULTS},
        "verdicts": dict(verdicts),
    }


# ---------------------------------------------------------------------------
# Outputs
# ---------------------------------------------------------------------------
def per_db_stats(cells: dict, ring_ids: set) -> dict:
    ring1: dict[str, int] = defaultdict(int)
    total: dict[str, int] = defaultdict(int)
    for cid, r in cells.items():
        for db in {o["db"] for o in r.get("organs", []) if o.get("kind") == "page"}:
            total[db] += 1
            if cid in ring_ids:
                ring1[db] += 1
    return {db: {"ring1": ring1[db], "cells_with_page": total[db],
                 "ring1_pct": round(100 * ring1[db] / total[db], 1),
                 "structural_zero": db in STRUCTURAL_ZERO_DBS}
            for db in sorted(total)}


def _md_escape(s: str) -> str:
    return s.replace("|", "\\|")


def write_md(path: Path, halo: dict) -> None:
    t = halo["totals"]
    per_db = halo["per_db"]
    cc = halo["crosscheck"]
    v = cc.get("verdicts", {})
    rows = halo["items"]

    db_lines = ["| DB | ring-1 cells | cells with a page organ | ring-1 % | note |",
                "|---|---:|---:|---:|---|"]
    for db, s in per_db.items():
        note = "structural zero — anchors via decl attributes" if s["structural_zero"] else ""
        db_lines.append(f"| {db} | {s['ring1']} | {s['cells_with_page']} | "
                        f"{s['ring1_pct']}% | {note} |")

    top_lines = ["| # | QID | label | DBs | depends-frac | centrality | verdict |",
                 "|---:|---|---|---|---|---:|---|"]
    for r in rows[:100]:
        if r["depends_frac"] is not None:
            fr = f"{r['depends_frac']:.3f} (n={r['depends_n']})"
        elif r["all_frac"] is not None:
            fr = f"all:{r['all_frac']:.3f} (n={r['all_n']})"
        else:
            fr = "isolated"
        verdict = r.get("verdict") or "—"
        if verdict == "false_positive" and r.get("existing_decl"):
            verdict = f"false_positive (`{r['existing_decl']}`)"
        top_lines.append(f"| {r['rank']} | {r['qid']} | {_md_escape(r['label'])} | "
                         f"{', '.join(r['dbs'])} | {fr} | {r['centrality_pct']:.1f} | "
                         f"{verdict} |")

    md = f"""# The Mathlib Halo — ring-1 gap analysis

*Generated by `manage/halo.py` on {halo['generated_at_iso']} from the Brain build
of {halo['brain_build']}. Every number below is re-derived from
`brain/data/{{cells,synapses}}.jsonl` at run time — re-run the script after a
Brain rebuild to refresh this report.*

## What the halo is

The Brain merges Wikidata concepts, Mathlib declarations, external-database
pages and WikiLean articles into cells. A **ring-1 halo cell** has at least one
external `page` organ (MathWorld, nLab, ProofWiki, EoM, PlanetMath, OEIS, LMFDB
knowls, …) but **zero `decl` organs**: the informal mathematical literature
names the object, Mathlib — as far as the Brain's tagging knows — does not.
The halo is therefore a map of *where the formal library is thin at the concept
level*, and its most-connected fringe is a candidate list for what to formalize
(or tag) next.

## Method

- **Extraction.** Ring-1 = cells with >=1 page organ and 0 decl organs
  ({t['ring1']} of {t['cells']} cells in this build).
- **Neighbor formalization.** Over cell<->cell synapses only (containment bonds
  to `path:` supercells excluded), each ring-1 cell gets two fractions: the
  share of formalized neighbors (>=1 decl organ) among **depends-bonded**
  neighbors (synapses whose kinds histogram has `depends` > 0 — QID-level
  Mathlib decl-dependency traces, the strongest signal) and among **all-bonded**
  neighbors (any kind: mentions, links, co-page, cites, relates, …).
- **Ranking.** Primary key = depends-fraction descending; a cell with no
  depends bond falls back to its all-bonds fraction (marked `frac_source:"all"`
  in `manage/data/halo.json`) instead of sinking below depends-frac-0 cells.
  Tiebreak = concept-graph PageRank (`manage/data/centrality.json`, joined by
  QID, missing -> 0) so that MathWorld recreational trivia with no structural
  pull does not bury the frontier; then label, for determinism.
- **Cross-check (top {cc['top_n']}).** Two probes per label: plausible
  CamelCase name forms against the decl-existence **union oracle**
  ({cc['oracle']['union_names']:,} names = doc-gen4 cache ∪ names parsed from
  the mathlib4 checkout — the same oracle as `manage/decl_existence_sweep.py`),
  and a **LeanSearch** semantic query (batched, throttled). Verdicts:
  `false_positive` = an oracle-verified decl matches a name form (named in the
  table — the concept is likely formalized, just untagged in the Brain);
  `possibly_tagged_gap` = fuzzy oracle or on-topic LeanSearch evidence exists,
  needs human review; `confirmed_gap` = both probes empty.

## Headline numbers (this build)

- Cells: **{t['cells']}** · formalized (>=1 decl organ): **{t['formalized_cells']}**
  · **ring-1 halo: {t['ring1']}**
- Ring-1 connectivity: {t['ring1_with_synapse']} have >=1 cell<->cell synapse,
  {t['ring1_with_depends_bond']} have a depends bond, {t['ring1_isolated']} are isolated.
- **{t['ring1_all_neighbors_formalized']}** ring-1 cells have *every* neighbor
  formalized — the tightest frontier.
- Top-{cc['top_n']} verdicts: **{v.get('confirmed_gap', 0)} confirmed_gap** ·
  {v.get('possibly_tagged_gap', 0)} possibly_tagged_gap ·
  {v.get('false_positive', 0)} false_positive.

## Per-database ring-1

{chr(10).join(db_lines)}

## Structural limitations — read before acting on the table

1. **The stacks / kerodon / erdos zeros are blindspots, not coverage.** Those
   sources enter the Brain *through* decl attributes (`@[stacks]`,
   `@[kerodon]`) or formal-conjectures reference URLs, so any cell carrying
   their page organ has a decl **by construction** and can never be ring-1.
   A Stacks-project tag with no Mathlib decl is invisible to this analysis.
   (dlmf's zero is just a tiny sample — 3 cells — not structural.)
2. **No decl organ ≠ not in Mathlib.** A ring-1 cell only means the Brain's
   tagging pipelines have not bonded a decl to it. The top-50 cross-check
   exists precisely because several "gaps" turn out to be
   already-formalized-but-untagged (the `false_positive` rows). Outside the
   checked top-{cc['top_n']}, assume the same contamination rate.
3. **Verdicts are lexical/heuristic.** An oracle name-form hit proves a decl
   *exists*, not that it means this concept (`residue` the local-ring map vs.
   the complex-analytic residue); LeanSearch relevance is judged by phrase
   overlap. Every verdict is a triage label for a human, not a ruling.
4. **Fractions with tiny neighborhoods are noisy.** all-frac = 1.0 over one
   neighbor is weak evidence; the `n` columns are part of the signal.
5. **Depends bonds on unformalized cells are QID-level.** A ring-1 cell has no
   decls of its own; its depends traces come from concept-graph dependency
   edges recorded between QIDs whose witnessing decls live in *neighboring*
   cells.
6. **Snapshot.** Everything reflects the Brain build named above and the
   mathlib4 checkout/doc-gen4 cache on disk at run time; Mathlib moves daily.

## Top 100 (of {t['ring1']}) — full ranked list in `manage/data/halo.json`

Verdicts beyond the top {cc['top_n']} are unchecked (—).

{chr(10).join(top_lines)}
"""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(md)


# ---------------------------------------------------------------------------
def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--top", type=int, default=50, help="rows to cross-check")
    ap.add_argument("--offline", action="store_true", help="skip LeanSearch")
    ap.add_argument("--cache-oracle-only", action="store_true",
                    help="skip the (slow) mathlib source parse; doc-gen4 cache only")
    ap.add_argument("--mathlib", type=Path, default=sweep.DEFAULT_MATHLIB)
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT)
    ap.add_argument("--md-out", type=Path, default=DEFAULT_MD)
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args()

    t0 = time.time()
    cells, build = load_brain()
    all_nb, dep_nb, n_cc = load_adjacency()
    cen = json.loads(CENTRALITY.read_text())["scores"] if CENTRALITY.exists() else {}
    if not cen:
        print("warning: centrality.json absent — centrality tiebreak is all-zero",
              file=sys.stderr)

    rows = build_ring(cells, all_nb, dep_nb, cen)
    ring_ids = {r["cell"] for r in rows}
    formalized_n = sum(1 for r in cells.values()
                       if any(o.get("kind") == "decl" for o in r.get("organs", [])))
    totals = {
        "cells": len(cells),
        "formalized_cells": formalized_n,
        "ring1": len(rows),
        "ring1_with_synapse": sum(1 for r in rows if not r["isolated"]),
        "ring1_with_depends_bond": sum(1 for r in rows if r["depends_n"] > 0),
        "ring1_isolated": sum(1 for r in rows if r["isolated"]),
        "ring1_all_neighbors_formalized": sum(
            1 for r in rows if r["all_n"] > 0 and r["all_formalized"] == r["all_n"]),
    }

    cc_meta = crosscheck(rows, args.top, args.offline, args.cache_oracle_only,
                         args.mathlib, args.quiet)

    halo = {
        "generated_at": int(t0),
        "generated_at_iso": time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime(t0)),
        "brain_build": build,
        "method": {
            "ring1": "cells with >=1 page organ and 0 decl organs",
            "neighbor": "cell<->cell synapses only; neighbor formalized iff >=1 decl organ",
            "depends_frac": "neighbors restricted to synapses with kinds.depends > 0",
            "ranking": "depends_frac desc (fallback all_frac, see frac_source), "
                       "tiebreak pagerank desc, then label",
            "verdicts": "false_positive = oracle-verified name form exists; "
                        "possibly_tagged_gap = fuzzy oracle or on-topic LeanSearch hit; "
                        "confirmed_gap = both probes empty",
            "structural_zero_dbs": list(STRUCTURAL_ZERO_DBS),
        },
        "inputs": {"cells": str(CELLS.relative_to(REPO)),
                   "synapses": str(SYNAPSES.relative_to(REPO)),
                   "centrality": str(CENTRALITY.relative_to(REPO)),
                   "n_cellcell_synapses": n_cc},
        "totals": totals,
        "per_db": per_db_stats(cells, ring_ids),
        "crosscheck": cc_meta,
        "items": rows,
    }

    args.out.parent.mkdir(parents=True, exist_ok=True)
    tmp = args.out.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(halo, ensure_ascii=False, indent=1))
    tmp.replace(args.out)
    write_md(args.md_out, halo)

    if not args.quiet:
        v = cc_meta.get("verdicts", {})
        print(f"mathlib halo  ({time.time() - t0:.1f}s)")
        print(f"  brain build {build} · {totals['cells']} cells · "
              f"ring-1 {totals['ring1']} ({totals['ring1_isolated']} isolated, "
              f"{totals['ring1_with_depends_bond']} with depends bonds, "
              f"{totals['ring1_all_neighbors_formalized']} fully-formalized neighborhoods)")
        print(f"  oracle union {cc_meta['oracle']['union_names']:,} names · "
              f"leansearch {'SKIPPED' if args.offline else 'queried'}")
        print(f"  top-{cc_meta['top_n']} verdicts: "
              f"{v.get('confirmed_gap', 0)} confirmed_gap · "
              f"{v.get('possibly_tagged_gap', 0)} possibly_tagged_gap · "
              f"{v.get('false_positive', 0)} false_positive")
        for r in rows[:10]:
            fr = (f"df={r['depends_frac']:.3f}" if r["depends_frac"] is not None
                  else f"af={r['all_frac']:.3f}" if r["all_frac"] is not None
                  else "isolated")
            print(f"    #{r['rank']:<3} {fr:<10} cen={r['centrality_pct']:5.1f} "
                  f"{r['qid'] or '-':<11} {r['label']:<32} "
                  f"[{r.get('verdict', '—')}]"
                  + (f" -> {r['existing_decl']}" if r.get("existing_decl") else ""))
        def rel(p: Path) -> str:
            return str(p.relative_to(REPO)) if p.is_relative_to(REPO) else str(p)
        print(f"  -> {rel(args.out)}")
        print(f"  -> {rel(args.md_out)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
