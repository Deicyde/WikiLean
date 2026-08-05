#!/usr/bin/env python3
"""Partition the HOMELESS cells (no decl organ) into named frontier AREAS.

THE BLOB: the bubbles view renders every cell with no decl organ as one grey
"no formal home" bucket (1,612 atoms as of 2026-08-01) — the single largest
thing on the root canvas, and the least legible. This builder gives each of
those atoms a deterministic home: the frontier AREA of the library territory
its synapses tie it to, so "Gompertz function" files under the Analysis
frontier instead of a 1,612-dot fog.

FRONTIER CONTRACT (pinned across agents; documented in brain/SCHEMA.md):

  brain/data/frontier.jsonl —
    line 1: {"_meta": {"generated_at", "method",
                       "counts": {"homeless", "assigned", "unsorted"},
                       "proximity": {"method", "lambda",
                                     "counts": {"direct", "bridged", "zero"}}}}
    then one row per area:
      {"id": "frontier:<Area>", "label", "cells": [cell ids], "n",
       "prox": {"db": [...], "dw": [...], "ib": [...], "iw": [...],
                "s": [...], "r": [...]},   # six arrays parallel to `cells`
       "near": "path:<Lib>/<Dir>"|null, "mean_stateability": float|null,
       "top": [up to 12 {"cell", "label", "score"}]}
    <Area> matches ^[A-Za-z][A-Za-z0-9_]{0,63}$.

  PARTITION: every homeless cell appears in EXACTLY ONE area — no drops, no
  dupes; counts reconcile (asserted here AND in brain/test_frontier.py).

  FORMAL PROXIMITY (PROXIMITY CONTRACT — replaces the 2026-08-02 halo hop
  shells, DESTROYED 2026-08-04 on Jack's call: hop counts made "1 jump over
  200 bonds" and "1 jump over one thread" the same tier). Every frontier cell
  gets a deterministic, bond-weighted score over RAW synapse weights (trace
  counts — no vote multipliers; this measures evidence mass, not vote
  strength):

      direct(c) = sum of the RAW weight of c's synapses into formalized
                  cells (>=1 decl organ; each synapse counted once)
      bridge(c) = sum over frontier neighbors u of min(w(c,u), direct(u))
      score(c)  = direct(c) + bridge(c) / 4

  One damped second-order term, lambda = 1/4 — exact in binary floats, so the
  build, the tests and the client can demand bit-for-bit equality. min() is
  the bottleneck rule: a bridge through u is worth no more than the bond to u
  AND no more than u's own direct evidence, so one isolated intermediary can
  never stand in for hundreds of direct bonds. MONOTONE: adding direct bonded
  weight raises the score by exactly that much; the bridge term never depends
  on the cell's own direct weight. Cells scoring 0 (no formal evidence within
  two hops) are counted LOUDLY, never silently.

  prox arrays (each parallel to the row's sorted `cells`):
    db = direct bonds     — # formalized neighbor cells            (int >= 0)
    dw = direct weight    — direct(c)                              (int >= 0)
    ib = bridging nbrs    — # frontier neighbors with direct(u)>0  (int >= 0)
    iw = bridge weight    — bridge(c)                              (int >= 0)
    s  = score            — dw + iw/4                       (exact 1/4-float)
    r  = radius percentile of s over ALL frontier cells: (#cells with
         strictly higher s + #cells with equal s / 2) / N, rounded to 4dp —
         RANK-based per the robust-fit rule (extremes cannot stretch the
         mapping), ties share r, 0 ~ most proximal. Computed at build time;
         the client never re-fits (for library subsets it re-ranks with this
         same one-line formula).

  ASSIGNMENT (deterministic, seedless, no LLM):
    1. weighted vote of the cell's synapse neighbors that have decl organs —
       each neighbor votes for its owning library area (the top-level dir of
       its supercells: Mathlib/<Dir> -> <Dir>, <Lib>/<Dir> -> <Lib>_<Dir>,
       bare <Lib> -> <Lib>); vote weight = per-kind trace count, with
       depends/invocation weighted 3x and mentions/links/relates/cites &c 1x;
       a neighbor spanning several areas splits its vote equally; winner by
       (-score, name) so ties break lexicographically.
    2. no formalized neighbors -> the cell's MSC top-level class, via an
       xref:msc:* ORGAN or a concept-organ xref EDGE (edges.jsonl) ->
       frontier:DeepFrontier_<MSCName> (lowest class code wins).
    3. else ONE round of label propagation over `relates`-kind synapses to
       homeless cells already assigned in phases 1-2 (majority by relates
       count; ties lexicographic).
    4. else frontier:Unsorted.

  FRONTIER GRAPH (brain/data/frontier_graph.json — the client-side re-scoring
  input for the Libraries toggle, shipped VERBATIM by build_cell_shards.py as
  site/assets/brain/cells/frontier_graph.json):
    {"_meta": {"generated_at" (the cell build's stamp), "method",
               "counts": {"cells", "formal", "edges", "libs"}},
     "cells":  [every homeless cell id, sorted] — the partition universe,
     "formal": {<cell>: {<RootSet>: summed RAW synapse weight, ...}} for
               every frontier cell with >=1 formalized neighbor. RootSet =
               the neighbor's owning library roots, "|"-joined sorted
               ("Mathlib", "Init|Mathlib") — an EXACT-set key, so restricting
               to a library subset never double-counts a multi-root neighbor
               (566 such rows on 2026-08-01 data). A root = the first path
               component of the neighbor's supercells; a supercell-less decl
               cell (Mathlib-archive names) falls back to its decl organ ids'
               <Lib> segment so attribution is TOTAL (asserted),
     "edges":  [[i, j, w], ...] index pairs into `cells` (i < j, sorted,
               deduped) with w = the synapse's RAW weight — every
               frontier<->frontier synapse}
  PARITY LAW: the client re-scores from an enabled-library set L:
      direct_L(c) = sum of formal[c][K] over key-sets K with K ∩ L != {}
      score_L(c)  = direct_L(c) + sum over edges (c,u,w) of
                    min(w, direct_L(u)) / 4
  and with ALL libraries enabled score_L MUST equal the shipped `s` EXACTLY
  (exact float equality — quarter-precision is lossless) — asserted here at
  build time AND spec-re-proved per cell in brain/test_frontier.py (F10),
  including under proper library subsets. Deterministic bytes (sorted keys
  and lists, input-pinned stamp).

  mean_stateability = mean of manage/data/halo.json items' all-bond
  neighbor-formalization (`all_frac`) over the area's cells present in
  halo.json — null if none. halo.json is OPTIONAL (fail-soft to null). KEPT
  because the areas view tints each frontier bubble by it
  (site/build_brain_page.py) — it is orthogonal to the destroyed hop shells.

  `top` = the area's 12 most-connected cells, score = the cell's total
  effective synapse weight (sum over its synapses of per-kind count x the
  same 3x/1x multipliers) — an integer, deterministic. (The vote's 3x
  multipliers are a RANKING device; the proximity score deliberately uses
  raw weights instead — two different questions.)

Reads  brain/data/{cells,synapses}.jsonl (required),
       brain/data/edges.jsonl (phase-2 msc xrefs; fail-soft),
       manage/data/halo.json (stateability; fail-soft).
Writes brain/data/frontier.jsonl + brain/data/frontier_graph.json
       (each atomic tmp+rename).

Every bound and every fallback tier LOGS what it drops and why — a silent
filter deciding what renders is the 'extreme minority' bug class.

Run: python3 brain/build_frontier.py     (after brain/build_cells.py)
"""
from __future__ import annotations

import json
import re
import sys
import time
from collections import Counter, defaultdict
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
CELLS_IN = HERE / "data" / "cells.jsonl"
SYNAPSES_IN = HERE / "data" / "synapses.jsonl"
EDGES_IN = HERE / "data" / "edges.jsonl"
HALO_IN = ROOT / "manage" / "data" / "halo.json"
OUT = HERE / "data" / "frontier.jsonl"
GRAPH_OUT = HERE / "data" / "frontier_graph.json"

AREA_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,63}$")
TOP_CAP = 12                      # contract: up to 12 `top` rows per area
KIND_MULT = {"depends": 3, "invocation": 3}   # everything else weighs 1
LAMBDA = 0.25    # the bridge damping — 1/4 is EXACT in binary floats (parity)

# MSC 2020 top-level classes -> contract-safe area names (DeepFrontier_<name>).
# Codes absent here fall back to MSC<code>, which still matches the id regex.
MSC_NAME = {
    "00": "General", "01": "History", "03": "Logic", "05": "Combinatorics",
    "06": "Order", "08": "GeneralAlgebra", "11": "NumberTheory",
    "12": "FieldTheory", "13": "CommutativeAlgebra", "14": "AlgebraicGeometry",
    "15": "LinearAlgebra", "16": "AssociativeRings", "17": "NonassociativeRings",
    "18": "CategoryTheory", "19": "KTheory", "20": "GroupTheory",
    "22": "TopologicalGroups", "26": "RealFunctions", "28": "MeasureTheory",
    "30": "ComplexFunctions", "31": "PotentialTheory",
    "32": "SeveralComplexVariables", "33": "SpecialFunctions",
    "34": "OrdinaryDifferentialEquations", "35": "PartialDifferentialEquations",
    "37": "DynamicalSystems", "39": "DifferenceEquations",
    "40": "SequencesSeries", "41": "Approximations", "42": "HarmonicAnalysis",
    "43": "AbstractHarmonicAnalysis", "44": "IntegralTransforms",
    "45": "IntegralEquations", "46": "FunctionalAnalysis", "47": "OperatorTheory",
    "49": "CalculusOfVariations", "51": "Geometry", "52": "ConvexGeometry",
    "53": "DifferentialGeometry", "54": "GeneralTopology",
    "55": "AlgebraicTopology", "57": "Manifolds", "58": "GlobalAnalysis",
    "60": "Probability", "62": "Statistics", "65": "NumericalAnalysis",
    "68": "ComputerScience", "70": "Mechanics", "74": "SolidMechanics",
    "76": "FluidMechanics", "78": "Optics", "80": "Thermodynamics",
    "81": "QuantumTheory", "82": "StatisticalMechanics", "83": "Relativity",
    "85": "Astronomy", "86": "Geophysics", "90": "OperationsResearch",
    "91": "GameTheoryEconomics", "92": "Biology", "93": "SystemsTheory",
    "94": "InformationTheory", "97": "MathematicsEducation",
}


def iter_jsonl(path: Path):
    """Yield data rows of a brain JSONL, skipping the leading `_meta` line."""
    with path.open() as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            row = json.loads(line)
            if "_meta" in row and len(row) == 1:
                continue
            yield row


def read_meta(path: Path) -> dict:
    with path.open() as fh:
        first = json.loads(next(fh))
    return first.get("_meta", {})


def camel_space(name: str) -> str:
    return re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", name)


def area_label(area: str, lib: str | None, top: str | None) -> str:
    """Human label for the bubble/panel. Deterministic, derived once."""
    if area == "Unsorted":
        return "Unsorted frontier"
    if area.startswith("DeepFrontier_"):
        return "Deep frontier · " + camel_space(area[len("DeepFrontier_"):])
    if lib and top and lib != "Mathlib":
        return f"{lib} · {top} frontier"
    return f"{(top or lib or area)} frontier"


def main() -> int:
    t0 = time.monotonic()
    for path in (CELLS_IN, SYNAPSES_IN):
        if not path.exists():
            raise SystemExit(f"missing {path} — run python3 brain/build_cells.py first")

    cells_meta = read_meta(CELLS_IN)
    generated_at = cells_meta.get("generated_at", "")  # input stamp, NOT wall clock:
    # the output must be byte-identical across reruns on the same inputs (tested)

    # ---- cells: who is formalized, who is homeless ---------------------------
    cells: dict[str, dict] = {}
    for row in iter_jsonl(CELLS_IN):
        cells[row["id"]] = row
    decl_cells = {cid for cid, c in cells.items()
                  if any(o.get("kind") == "decl" for o in c.get("organs", []))}
    homeless = sorted(set(cells) - decl_cells)
    print(f"cells: {len(cells)} total, {len(decl_cells)} with a decl organ, "
          f"{len(homeless)} HOMELESS (the partition universe)")

    def areas_of(cid: str) -> list[str]:
        """Distinct areas a formalized cell's supercells map to (contract 3)."""
        out = set()
        for sup in cells[cid].get("supercells", []):
            parts = sup.split(":", 1)[1].split("/")
            lib, top = parts[0], (parts[1] if len(parts) > 1 else None)
            if lib == "Mathlib":
                out.add(top if top else "Mathlib")
            else:
                out.add(f"{lib}_{top}" if top else lib)
        return sorted(out)

    # ---- synapses: undirected adjacency with effective weights ----------------
    nbrs: dict[str, list] = defaultdict(list)
    n_syn = 0
    homeless_set = set(homeless)
    # raw (src, dst, weight) rows touching >=1 homeless endpoint — the frontier
    # graph's input (frontier<->frontier edges + formalized-neighbor weights)
    raw_frontier_syn: list[tuple[str, str, int]] = []
    for row in iter_jsonl(SYNAPSES_IN):
        n_syn += 1
        kinds = row.get("kinds", {})
        eff = sum(cnt * KIND_MULT.get(k, 1) for k, cnt in kinds.items())
        nbrs[row["src"]].append((row["dst"], kinds, eff))
        nbrs[row["dst"]].append((row["src"], kinds, eff))
        if row["src"] in homeless_set or row["dst"] in homeless_set:
            raw_frontier_syn.append((row["src"], row["dst"], row["weight"]))
    print(f"synapses: {n_syn} rows loaded")

    # area registry: name -> {"near": path|None, "lib", "top"}, precomputed from
    # EVERY formalized cell's supercells so a vote winner always knows its formal
    # home. If two libraries ever yield the same area name, the lexicographically
    # smallest near-path wins — counted, never silent.
    areas: dict[str, dict] = {}
    near_conflicts = 0

    def register(area: str, lib: str | None, top: str | None) -> None:
        nonlocal near_conflicts
        near = f"path:{lib}" + (f"/{top}" if top else "") if lib else None
        if area not in areas:
            areas[area] = {"near": near, "lib": lib, "top": top}
        elif near and areas[area]["near"] and near != areas[area]["near"]:
            near_conflicts += 1
            if near < areas[area]["near"]:
                areas[area] = {"near": near, "lib": lib, "top": top}

    for cid in sorted(decl_cells):
        for sup in cells[cid].get("supercells", []):
            parts = sup.split(":", 1)[1].split("/")
            lib, top = parts[0], (parts[1] if len(parts) > 1 else None)
            if lib == "Mathlib":
                register(top if top else "Mathlib", lib, top)
            else:
                register(f"{lib}_{top}" if top else lib, lib, top)

    # ---- phase 1: the weighted vote ------------------------------------------
    assigned: dict[str, str] = {}
    tier: dict[str, str] = {}
    ties = 0
    multi_area_votes = 0
    only_supercell_nbrs = 0
    formal_but_arealess = 0
    for cid in homeless:
        votes: Counter = Counter()
        n_formal = 0
        saw_supercell = False
        for other, kinds, eff in nbrs.get(cid, []):
            if other.startswith("path:"):
                saw_supercell = True     # a rule-5 supercell endpoint never votes
                continue
            if other not in decl_cells:
                continue                 # informal neighbors do not vote in phase 1
            n_formal += 1
            ars = areas_of(other)
            if len(ars) > 1:
                multi_area_votes += 1
            for a in ars:
                votes[a] += eff / len(ars)
        if n_formal == 0:
            if saw_supercell:
                only_supercell_nbrs += 1
            continue
        if not votes:
            # formalized neighbors exist but none has a supercell to vote for
            # (e.g. archive decls outside the module tree) — fall through to
            # the later tiers like any other zero-signal cell, counted loudly.
            formal_but_arealess += 1
            continue
        # quantize before ranking so near-ties from split-vote float noise
        # (3×(1/3) vs 1.0) genuinely break lexicographically, as the log says
        ranked = sorted(votes.items(), key=lambda kv: (-round(kv[1], 9), kv[0]))
        if len(ranked) > 1 and round(ranked[0][1], 9) == round(ranked[1][1], 9):
            ties += 1
        assigned[cid] = ranked[0][0]   # already in the registry (precomputed)
        tier[cid] = "vote"
    voted = set(assigned)
    zero_formal = [c for c in homeless if c not in voted]
    isolated = [c for c in zero_formal if not nbrs.get(c)]
    print(f"\nphase 1 (weighted vote): {len(voted)} assigned "
          f"({ties} top-score ties broken lexicographically; "
          f"{multi_area_votes} multi-area neighbor votes split)")
    print(f"  fell through: {len(zero_formal)} cells have ZERO formalized "
          f"neighbors — {only_supercell_nbrs} touch only supercell (path:) "
          f"endpoints, {len(isolated)} have no synapses at all"
          + (f"; {formal_but_arealess} have formalized neighbors with no "
             f"votable area (fell through to later tiers)"
             if formal_but_arealess else ""))

    # ---- phase 2: MSC top-level class ----------------------------------------
    msc_by_concept: dict[str, set] = defaultdict(set)
    if EDGES_IN.exists():
        for row in iter_jsonl(EDGES_IN):
            if row.get("kind") == "xref" and \
                    str(row.get("dst", "")).startswith("xref:msc:"):
                msc_by_concept[row["src"]].add(row["dst"][len("xref:msc:"):])
    else:
        print(f"  ! {EDGES_IN} missing — phase 2 sees msc ORGANS only (fail-soft)")
    n_organ_hits = 0
    n_msc = 0
    for cid in zero_formal:
        codes: set[str] = set()
        for o in cells[cid].get("organs", []):
            oid = o.get("id", "")
            if oid.startswith("xref:msc:"):
                n_organ_hits += 1
                codes.add(oid[len("xref:msc:"):])
            codes |= msc_by_concept.get(oid, set())
        if not codes:
            continue
        top_class = sorted(c[:2] for c in codes)[0]   # deterministic: lowest class
        area = "DeepFrontier_" + MSC_NAME.get(top_class, f"MSC{top_class}")
        assigned[cid] = area
        tier[cid] = "msc"
        register(area, None, None)
        n_msc += 1
    no_msc = [c for c in zero_formal if c not in assigned]
    print(f"phase 2 (MSC xref): {n_msc} assigned "
          f"({n_organ_hits} via an xref:msc organ, the rest via concept-organ "
          f"xref edges)")
    print(f"  fell through: {len(no_msc)} cells carry no msc xref at all")

    # ---- phase 3: one round of relates-propagation ---------------------------
    pre_assigned = dict(assigned)   # phases 1-2, FROZEN for the single round
    n_rel = 0
    had_relates_but_unreachable = 0
    no_relates_at_all = 0
    for cid in no_msc:
        votes = Counter()
        any_relates = False
        for other, kinds, _eff in nbrs.get(cid, []):
            if "relates" not in kinds:
                continue
            any_relates = True
            if other in pre_assigned:
                votes[pre_assigned[other]] += kinds["relates"]
        if votes:
            assigned[cid] = sorted(votes.items(), key=lambda kv: (-kv[1], kv[0]))[0][0]
            tier[cid] = "relates"
            n_rel += 1
        elif any_relates:
            had_relates_but_unreachable += 1
        else:
            no_relates_at_all += 1
    print(f"phase 3 (one relates hop to a phase-1/2-assigned homeless cell): "
          f"{n_rel} assigned")
    print(f"  fell through: {had_relates_but_unreachable} have relates bonds "
          f"but none to an assigned homeless cell; {no_relates_at_all} have no "
          f"relates bond at all")

    # ---- phase 4: Unsorted ----------------------------------------------------
    unsorted = [c for c in homeless if c not in assigned]
    for cid in unsorted:
        assigned[cid] = "Unsorted"
        tier[cid] = "unsorted"
    register("Unsorted", None, None)
    n_unsorted_isolated = sum(1 for c in unsorted if not nbrs.get(c))
    print(f"phase 4: {len(unsorted)} Unsorted "
          f"({n_unsorted_isolated} fully synapse-less, "
          f"{len(unsorted) - n_unsorted_isolated} connected but unreachable)")
    if near_conflicts:
        print(f"  ! {near_conflicts} near-path conflicts (two libraries named the "
              f"same area) — kept the lexicographically smallest path")

    # ---- reconciliation (the partition IS the contract) ----------------------
    n_by_tier = Counter(tier.values())
    assert set(assigned) == set(homeless), "partition lost or invented cells"
    assert len(assigned) == len(homeless), "partition duplicated a cell"
    total = sum(n_by_tier.values())
    print(f"\nRECONCILE: vote {n_by_tier['vote']} + msc {n_by_tier['msc']} + "
          f"relates {n_by_tier['relates']} + unsorted {n_by_tier['unsorted']} = "
          f"{total} == homeless {len(homeless)}")
    assert total == len(homeless)

    # ---- the frontier graph's primitives (also the score's inputs) -----------
    # One pass over the raw frontier-touching synapse rows yields BOTH the
    # graph file's payload and the proximity score's inputs, so the shipped
    # score and the client's recomputation can never diverge structurally.
    cell_index = {c: i for i, c in enumerate(homeless)}   # homeless is sorted
    lib_memo: dict[str, list[str]] = {}

    def libs_of(cid: str) -> list[str]:
        """Owning library roots of a formalized cell: the first path component
        of each supercell; a supercell-less decl cell (Mathlib-archive names
        like Theorems100.*) falls back to its decl organ ids' <Lib> segment,
        so EVERY decl cell yields >=1 lib — parity needs total attribution."""
        got = lib_memo.get(cid)
        if got is None:
            libs = {sup.split(":", 1)[1].split("/")[0]
                    for sup in cells[cid].get("supercells") or []}
            if not libs:
                libs = {o["id"].split(":", 2)[1]
                        for o in cells[cid].get("organs", [])
                        if o.get("kind") == "decl" and o["id"].count(":") >= 2}
            got = lib_memo[cid] = sorted(libs)
        return got

    formal: dict[str, Counter] = defaultdict(Counter)  # cid -> RootSet key -> w
    direct_w: Counter = Counter()   # cid -> summed RAW weight into decl cells
    direct_b: Counter = Counter()   # cid -> # formalized neighbor cells
    edge_w: dict[tuple[int, int], int] = {}   # (i, j) i<j -> RAW weight
    n_fallback_rows = 0
    fallback_cells: set[str] = set()
    n_unattributable = 0
    n_self = 0
    for src, dst, w in raw_frontier_syn:
        si, di = cell_index.get(src), cell_index.get(dst)
        if si is not None and di is not None:      # frontier <-> frontier
            if si == di:
                n_self += 1                        # a C6 breach — never silent
                continue
            key = (si, di) if si < di else (di, si)
            edge_w[key] = edge_w.get(key, 0) + w   # dedupe-by-sum (order-free)
            continue
        cid, other = (src, dst) if si is not None else (dst, src)
        if other not in decl_cells:
            continue        # the partner is a path: supercell endpoint (rule 5)
        libs = libs_of(other)
        if not cells[other].get("supercells"):
            n_fallback_rows += 1
            fallback_cells.add(other)
        if not libs:
            n_unattributable += 1   # would break parity; asserted red below
            continue
        formal[cid]["|".join(libs)] += w   # EXACT root-set key (no double count)
        direct_w[cid] += w
        direct_b[cid] += 1
    assert not n_unattributable, \
        (f"{n_unattributable} formalized-neighbor rows carry NO attributable "
         f"library (no supercell, no decl organ) — attribution must be TOTAL "
         f"or the parity law breaks")
    for c in formal:   # structural: the score's direct component IS the graph's
        assert direct_w[c] == sum(formal[c].values()), \
            f"direct weight != formal row sum on {c} (attribution drift)"

    # ---- FORMAL PROXIMITY: score every frontier cell -------------------------
    ff_nbrs: dict[int, list[tuple[int, int]]] = defaultdict(list)
    for (i, j), w in edge_w.items():
        ff_nbrs[i].append((j, w))
        ff_nbrs[j].append((i, w))
    bridge_w: dict[str, int] = {}
    bridge_b: dict[str, int] = {}
    for c in homeless:
        iw = ib = 0
        for jn, w in ff_nbrs.get(cell_index[c], []):
            du = direct_w.get(homeless[jn], 0)
            if du:
                iw += min(w, du)   # the bottleneck rule
                ib += 1
        bridge_w[c] = iw
        bridge_b[c] = ib
    score = {c: direct_w.get(c, 0) + bridge_w[c] * LAMBDA for c in homeless}

    # radius: midrank percentile of the score over the WHOLE frontier
    # population — rank-based (the robust-fit rule: a 900-weight hub cannot
    # stretch the mapping), ties share r, deterministic.
    n_home = len(homeless)
    by_score = Counter(score.values())
    higher: dict[float, int] = {}
    acc = 0
    for v in sorted(by_score, reverse=True):
        higher[v] = acc
        acc += by_score[v]
    radius = {c: round((higher[score[c]] + by_score[score[c]] / 2) / n_home, 4)
              for c in homeless}

    n_direct = sum(1 for c in homeless if direct_w.get(c, 0) > 0)
    n_bridged = sum(1 for c in homeless
                    if direct_w.get(c, 0) == 0 and bridge_w[c] > 0)
    n_zero = n_home - n_direct - n_bridged
    zero_with_syn = sum(1 for c in homeless
                        if score[c] == 0 and nbrs.get(c))
    smax = max(score.values()) if score else 0
    print(f"\nformal proximity (score = direct + bridge/4, raw weights): "
          f"{n_direct} cells bond the core directly, {n_bridged} only bridge "
          f"through frontier neighbors, {n_zero} score 0")
    print(f"  zero-scored accounting: {zero_with_syn} of the {n_zero} still "
          f"carry synapses (no formal evidence within two hops), "
          f"{n_zero - zero_with_syn} are fully synapse-less; max score {smax}")
    assert all(v >= 0 and v == v for v in score.values()), \
        "negative or NaN proximity score"

    # ---- halo join: mean_stateability ----------------------------------------
    halo_frac: dict[str, float] = {}
    if HALO_IN.exists():
        try:
            halo = json.loads(HALO_IN.read_text())
            for item in halo.get("items", []):
                if item.get("all_frac") is not None:
                    halo_frac[item["cell"]] = item["all_frac"]
            joined = sum(1 for c in homeless if c in halo_frac)
            print(f"halo.json: {len(halo.get('items', []))} items; {joined}/"
                  f"{len(homeless)} homeless cells join with a non-null all_frac "
                  f"({100 * joined / max(len(homeless), 1):.1f}%)")
        except (json.JSONDecodeError, OSError) as exc:
            print(f"  ! halo.json unreadable ({exc}) — mean_stateability null "
                  f"everywhere (fail-soft)")
    else:
        print(f"  ! {HALO_IN} missing — mean_stateability null everywhere "
              f"(fail-soft)")

    # ---- rows ----------------------------------------------------------------
    members: dict[str, list[str]] = defaultdict(list)
    for cid in homeless:
        members[assigned[cid]].append(cid)

    def cell_score(cid: str) -> int:
        return sum(eff for _o, _k, eff in nbrs.get(cid, []))

    rows = []
    for area in sorted(members):
        assert AREA_RE.match(area), f"area name breaks the contract regex: {area}"
        info = areas.get(area) or {"near": None, "lib": None, "top": None}
        cell_ids = sorted(members[area])
        fracs = [halo_frac[c] for c in cell_ids if c in halo_frac]
        top = sorted(((cell_score(c), c) for c in cell_ids),
                     key=lambda sc: (-sc[0], sc[1]))[:TOP_CAP]
        # prox: the six PROXIMITY CONTRACT arrays, parallel to cell_ids
        # (sorted). Every member gets a value — never a hole, never a drop.
        prox = {"db": [direct_b.get(c, 0) for c in cell_ids],
                "dw": [direct_w.get(c, 0) for c in cell_ids],
                "ib": [bridge_b[c] for c in cell_ids],
                "iw": [bridge_w[c] for c in cell_ids],
                "s": [score[c] for c in cell_ids],
                "r": [radius[c] for c in cell_ids]}
        rows.append({
            "id": f"frontier:{area}",
            "label": area_label(area, info["lib"], info["top"]),
            "cells": cell_ids,
            "n": len(cell_ids),
            "prox": prox,
            "near": info["near"],
            "mean_stateability":
                round(sum(fracs) / len(fracs), 4) if fracs else None,
            "top": [{"cell": c, "label": cells[c].get("label") or c, "score": s}
                    for s, c in top],
        })
    rows.sort(key=lambda r: (-r["n"], r["id"]))

    n_state = sum(1 for r in rows if r["mean_stateability"] is not None)
    print(f"\nareas: {len(rows)} ({n_state} with mean_stateability, "
          f"{len(rows) - n_state} null — no halo-joined member)")
    print(f"{'area':<44} {'n':>5}  {'near':<36} state  "
          f"direct/bridged/zero  max_s")
    for r in rows:
        state = ("-" if r["mean_stateability"] is None
                 else f"{r['mean_stateability']:.3f}")
        p = r["prox"]
        nd = sum(1 for v in p["dw"] if v > 0)
        nb = sum(1 for dwv, iwv in zip(p["dw"], p["iw"]) if dwv == 0 and iwv > 0)
        print(f"{r['id']:<44} {r['n']:>5}  {r['near'] or '-':<36} {state:<6} "
              f"{nd}/{nb}/{r['n'] - nd - nb}  {max(p['s']):g}")

    # ---- write (atomic) ------------------------------------------------------
    meta = {"_meta": {
        "generated_at": generated_at,
        "method": "synapse-vote(depends/invocation x3) -> msc-xref -> "
                  "relates-propagation -> unsorted; deterministic, seedless, "
                  "no LLM (FRONTIER CONTRACT, brain/SCHEMA.md)",
        "counts": {"homeless": len(homeless),
                   "assigned": len(homeless) - len(unsorted),
                   "unsorted": len(unsorted)},
        "proximity": {
            "method": "score = direct + bridge/4 over RAW synapse weights: "
                      "direct = summed weight of the cell's synapses into "
                      "formalized (decl-organ) cells; bridge = sum over "
                      "frontier neighbors u of min(bond weight, direct(u)) — "
                      "one damped second-order term, bottleneck-capped so an "
                      "isolated intermediary never stands in for direct "
                      "bonds; r = midrank percentile of the score "
                      "(rank-robust, ties share)",
            "lambda": LAMBDA,
            "counts": {"direct": n_direct, "bridged": n_bridged,
                       "zero": n_zero}},
        "phases": {"vote": n_by_tier["vote"], "msc": n_by_tier["msc"],
                   "relates": n_by_tier["relates"],
                   "unsorted": n_by_tier["unsorted"]},
        "inputs": {"cells": len(cells), "synapses": n_syn,
                   "halo_joined": sum(1 for c in homeless if c in halo_frac)},
    }}
    tmp = OUT.with_suffix(".jsonl.tmp")
    with tmp.open("w") as fh:
        fh.write(json.dumps(meta, ensure_ascii=False, separators=(",", ":")) + "\n")
        for r in rows:
            fh.write(json.dumps(r, ensure_ascii=False, separators=(",", ":")) + "\n")
    tmp.rename(OUT)
    print(f"\n-> {OUT} ({len(rows)} areas over {len(homeless)} homeless cells) "
          f"in {time.monotonic() - t0:.1f}s")

    # ---- frontier graph: the client-side re-scoring input --------------------
    # FRONTIER GRAPH contract (docstring above; brain/SCHEMA.md): `cells` =
    # every homeless cell, sorted; `formal` = per frontier cell with >=1
    # formalized neighbor, {"|"-joined sorted root set -> summed RAW weight};
    # `edges` = every frontier<->frontier synapse as [i, j, w]. The client
    # re-scores from an enabled-library set; with ALL libraries enabled the
    # result must equal the shipped `s` EXACTLY (PARITY LAW) — asserted right
    # here, before the file can ship.
    lib_cells: Counter = Counter()
    for lib_row in formal.values():
        roots = {root for key in lib_row for root in key.split("|")}
        for root in roots:      # each adjacent cell counts ONCE per root
            lib_cells[root] += 1
    print(f"\nfrontier graph: {len(homeless)} cells, {len(formal)} with a "
          f"formalized neighbor (direct > 0), "
          f"{len(homeless) - len(formal)} without, "
          f"{len(edge_w)} frontier<->frontier edges")
    print("  formal libs: " + ", ".join(
        f"{lib}: {lib_cells[lib]} cells" for lib in sorted(lib_cells)))
    n_multi = sum(1 for lib_row in formal.values()
                  for key in lib_row if "|" in key)
    if n_multi:
        print(f"  {n_multi} formal entries use a multi-root key (exact-set "
              f"attribution — a subset restriction never double-counts them)")
    if fallback_cells:
        print(f"  {n_fallback_rows} rows attributed via the decl-id fallback "
              f"({len(fallback_cells)} supercell-less decl cells, e.g. "
              f"{sorted(fallback_cells)[:3]})")
    if n_self:
        print(f"  ! {n_self} self-loop synapse rows SKIPPED (src == dst — C6 "
              f"should forbid this; check brain/test_cells.py)")

    edges_out = sorted(([i, j, w] for (i, j), w in edge_w.items()))
    graph = {
        "_meta": {
            "generated_at": generated_at,   # the cell build's stamp (determinism)
            "method": "cells = the homeless partition universe, sorted; formal "
                      "= per-cell {root set: summed RAW synapse weight} over "
                      "decl-organ neighbors, keyed by the neighbor's EXACT "
                      "'|'-joined sorted library roots (root = first supercell "
                      "path component; supercell-less decl cells fall back to "
                      "the decl id's <Lib> segment) so a subset restriction "
                      "never double-counts a multi-root neighbor; edges = "
                      "every frontier<->frontier synapse as [i,j,w] into "
                      "cells, i<j, w = RAW weight. Client re-score for lib "
                      "set L: direct_L = sum of formal entries whose key "
                      "intersects L; score_L = direct_L + sum(min(w, "
                      "direct_L(u)))/4 over edges; all libs enabled == the "
                      "shipped s EXACTLY (PARITY LAW, asserted at build + "
                      "test_frontier F10)",
            "counts": {"cells": len(homeless), "formal": len(formal),
                       "edges": len(edges_out),
                       "libs": {lib: lib_cells[lib] for lib in sorted(lib_cells)}},
        },
        "cells": homeless,
        "formal": {c: {k: formal[c][k] for k in sorted(formal[c])}
                   for c in sorted(formal)},
        "edges": edges_out,
    }

    # PARITY LAW, asserted at build time from the GRAPH OBJECT alone (the
    # client's view of the world): formal's keys are exactly the direct>0
    # cells, and the all-libraries re-score reproduces the shipped `s`
    # bit-for-bit on every cell.
    d_pos = {c for c in homeless if direct_w.get(c, 0) > 0}
    assert set(graph["formal"]) == d_pos, \
        (f"formal keys != the direct>0 cells: "
         f"{len(set(graph['formal']) - d_pos)} extra, "
         f"{len(d_pos - set(graph['formal']))} missing")
    g_direct = {c: sum(row.values()) for c, row in graph["formal"].items()}
    g_adj: dict[int, list[tuple[int, int]]] = defaultdict(list)
    for i, j, w in graph["edges"]:
        g_adj[i].append((j, w))
        g_adj[j].append((i, w))
    parity_bad = []
    for c in homeless:
        acc2 = 0
        for jn, w in g_adj.get(cell_index[c], []):
            du = g_direct.get(homeless[jn], 0)
            if du:
                acc2 += min(w, du)
        if g_direct.get(c, 0) + acc2 * LAMBDA != score[c]:
            parity_bad.append(c)
    assert not parity_bad, \
        (f"PARITY LAW broken: the client re-score over the graph diverges "
         f"from the shipped scores on {len(parity_bad)} cells, "
         f"e.g. {parity_bad[:3]}")
    print("  parity law holds: all-libraries client re-score == shipped s "
          f"({len(homeless)} cells, exact float equality)")

    gtmp = GRAPH_OUT.with_suffix(".json.tmp")
    gtmp.write_text(json.dumps(graph, ensure_ascii=False, separators=(",", ":")))
    gtmp.rename(GRAPH_OUT)
    print(f"-> {GRAPH_OUT} ({GRAPH_OUT.stat().st_size / 1000:.0f} KB)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
