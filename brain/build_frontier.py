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
                       "halo": {"shell_counts", "method"}}}
    then one row per area:
      {"id": "frontier:<Area>", "label", "cells": [cell ids], "n",
       "shells": {"1": [ids], "2": [ids], "3": [ids], "disc": [ids]},
       "near": "path:<Lib>/<Dir>"|null, "mean_stateability": float|null,
       "top": [up to 12 {"cell", "label", "score"}]}
    <Area> matches ^[A-Za-z][A-Za-z0-9_]{0,63}$.

  PARTITION: every homeless cell appears in EXACTLY ONE area — no drops, no
  dupes; counts reconcile (asserted here AND in brain/test_frontier.py).

  HALO SHELLS (the halo view's data): a multi-source BFS from ALL formalized
  cells (>=1 decl organ) over cell<->cell synapses — every kind conducts,
  path: (supercell) endpoints never do — gives every homeless cell a hop
  distance d to the nearest formalized cell. Each area row's "shells" is a
  partition of that area's "cells" keyed by str(d) ("disc" = unreachable;
  empty shell keys omitted; ids sorted). _meta.halo.shell_counts is the
  global tally (855/454/13/290 on 2026-08-01 data) and must equal the sum
  over rows — asserted here AND spec-re-derived in brain/test_frontier.py.

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

  mean_stateability = mean of manage/data/halo.json items' all-bond
  neighbor-formalization (`all_frac`) over the area's cells present in
  halo.json — null if none. halo.json is OPTIONAL (fail-soft to null).

  `top` = the area's 12 most-connected cells, score = the cell's total
  effective synapse weight (sum over its synapses of per-kind count x the
  same 3x/1x multipliers) — an integer, deterministic.

Reads  brain/data/{cells,synapses}.jsonl (required),
       brain/data/edges.jsonl (phase-2 msc xrefs; fail-soft),
       manage/data/halo.json (stateability; fail-soft).
Writes brain/data/frontier.jsonl (atomic tmp+rename).

Every bound and every fallback tier LOGS what it drops and why — a silent
filter deciding what renders is the 'extreme minority' bug class.

Run: python3 brain/build_frontier.py     (after brain/build_cells.py)
"""
from __future__ import annotations

import json
import re
import sys
import time
from collections import Counter, defaultdict, deque
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
CELLS_IN = HERE / "data" / "cells.jsonl"
SYNAPSES_IN = HERE / "data" / "synapses.jsonl"
EDGES_IN = HERE / "data" / "edges.jsonl"
HALO_IN = ROOT / "manage" / "data" / "halo.json"
OUT = HERE / "data" / "frontier.jsonl"

AREA_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,63}$")
TOP_CAP = 12                      # contract: up to 12 `top` rows per area
KIND_MULT = {"depends": 3, "invocation": 3}   # everything else weighs 1

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
    for row in iter_jsonl(SYNAPSES_IN):
        n_syn += 1
        kinds = row.get("kinds", {})
        eff = sum(cnt * KIND_MULT.get(k, 1) for k, cnt in kinds.items())
        nbrs[row["src"]].append((row["dst"], kinds, eff))
        nbrs[row["dst"]].append((row["src"], kinds, eff))
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

    # ---- halo BFS: hop distance to the formalized interior -------------------
    # Multi-source BFS from EVERY formalized cell (>=1 decl organ) over
    # cell<->cell synapses. ALL kinds conduct (this measures reachability, not
    # vote strength, so no 3x weighting); path: endpoints are supercells, not
    # cells, and never conduct. d = hops from a homeless cell to the nearest
    # formalized cell; unreachable cells are "disc". Deterministic: BFS layers
    # are order-independent and the seed set is iterated sorted anyway.
    dist: dict[str, int] = {cid: 0 for cid in decl_cells}
    bfs_queue = deque(sorted(decl_cells))
    while bfs_queue:
        cur = bfs_queue.popleft()
        for other, _kinds, _eff in nbrs.get(cur, []):
            if other in cells and other not in dist:
                dist[other] = dist[cur] + 1
                bfs_queue.append(other)

    def shell_of(cid: str) -> str:
        d = dist.get(cid)
        return "disc" if d is None else str(d)

    shell_counts = Counter(shell_of(c) for c in homeless)
    # partition set-math: every homeless cell lands in exactly one shell
    assert sum(shell_counts.values()) == len(homeless), \
        "halo shells lost or invented cells"
    shell_keys = sorted((k for k in shell_counts if k != "disc"), key=int) \
        + (["disc"] if "disc" in shell_counts else [])
    deep = [k for k in shell_keys if k != "disc" and int(k) > 3]
    print("halo BFS (all-kinds cell<->cell synapses, path: endpoints excluded): "
          + ", ".join(f"d={k}: {shell_counts[k]}" if k != "disc"
                      else f"disconnected: {shell_counts[k]}"
                      for k in shell_keys)
          + f"; sums to {sum(shell_counts.values())} == homeless")
    if deep:
        print(f"  ! {sum(shell_counts[k] for k in deep)} cells sit DEEPER than "
              f"d=3 (shells {deep}) — the halo view's 3-ring layout will need "
              f"a look")

    # ---- halo join: mean_stateability ----------------------------------------
    halo_frac: dict[str, float] = {}
    if HALO_IN.exists():
        try:
            halo = json.loads(HALO_IN.read_text())
            for item in halo.get("items", []):
                if item.get("all_frac") is not None:
                    halo_frac[item["cell"]] = item["all_frac"]
            joined = sum(1 for c in homeless if c in halo_frac)
            print(f"halo: {len(halo.get('items', []))} items; {joined}/"
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
        # shells: this area's slice of the halo BFS — a PARTITION of cell_ids
        # (asserted), keyed "1"/"2"/…/"disc", empty keys omitted, ids sorted
        # (cell_ids is sorted, so per-shell append order stays sorted).
        by_shell: dict[str, list[str]] = defaultdict(list)
        for c in cell_ids:
            by_shell[shell_of(c)].append(c)
        assert sum(len(v) for v in by_shell.values()) == len(cell_ids) \
            and set().union(*by_shell.values()) == set(cell_ids), \
            f"shells do not partition {area}"
        area_shell_keys = sorted((k for k in by_shell if k != "disc"), key=int) \
            + (["disc"] if "disc" in by_shell else [])
        rows.append({
            "id": f"frontier:{area}",
            "label": area_label(area, info["lib"], info["top"]),
            "cells": cell_ids,
            "n": len(cell_ids),
            "shells": {k: by_shell[k] for k in area_shell_keys},
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
          f"shells d1/d2/d3/disc")
    for r in rows:
        state = ("-" if r["mean_stateability"] is None
                 else f"{r['mean_stateability']:.3f}")
        sh = "/".join(str(len(r["shells"].get(k, []))) for k in ("1", "2", "3",
                                                                "disc"))
        print(f"{r['id']:<44} {r['n']:>5}  {r['near'] or '-':<36} {state:<6} "
              f"{sh}")

    # ---- write (atomic) ------------------------------------------------------
    meta = {"_meta": {
        "generated_at": generated_at,
        "method": "synapse-vote(depends/invocation x3) -> msc-xref -> "
                  "relates-propagation -> unsorted; deterministic, seedless, "
                  "no LLM (FRONTIER CONTRACT, brain/SCHEMA.md)",
        "counts": {"homeless": len(homeless),
                   "assigned": len(homeless) - len(unsorted),
                   "unsorted": len(unsorted)},
        "halo": {"shell_counts": {k: shell_counts[k] for k in shell_keys},
                 "method": "multi-source BFS from all decl-organ cells over "
                           "cell<->cell synapses (all kinds conduct; path: "
                           "endpoints excluded); d = hops to the nearest "
                           "formalized cell, disc = unreachable"},
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
    return 0


if __name__ == "__main__":
    sys.exit(main())
