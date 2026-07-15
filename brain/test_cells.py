#!/usr/bin/env python3
"""Acceptance for the BRAIN v3 cell layer — C1..C7 of `brain/SCHEMA.md`.

These are not unit tests of the plumbing; they are the CONTRACT. Each one pins a
property that the merge function must have, stated as a datapoint on live data:

  C1  the Module atom really is Jack's atom (Module + Vector space + decl Module)
  C2  Euclidean space stays a SEPARATE atom  <- the anti-chaining guarantee
  C3  no cell is a blob                      <- the merge rule did not degenerate
  C4  every organ resolves to exactly one cell (the aliases.json contract)
  C5  a page claimed by >1 cell is a supercell organ, never a cell organ
  C6  every synapse has >=1 trace and no cell pair is duplicated
  C7  rejected tag-queue claims never bond; queued tags stay distinguishable

Run: python3 brain/test_cells.py        (exit 0 = green; the nightly aborts on red)
"""
from __future__ import annotations

import sys
from collections import Counter
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from build_cells import (ATTACH, CELL_MAX_ORGANS, FUSE, build,  # noqa: E402
                         load_rejected)

FAILURES: list[str] = []
CHECKS = 0


def check(name: str, ok: bool, detail: str = "") -> None:
    global CHECKS
    CHECKS += 1
    if ok:
        print(f"  \033[32mPASS\033[0m {name}")
    else:
        print(f"  \033[31mFAIL\033[0m {name}: {detail}")
        FAILURES.append(f"{name}: {detail}")


def organ_ids(cell: dict, kind: str | None = None) -> set[str]:
    return {o["id"] for o in cell["organs"] if kind is None or o["kind"] == kind}


def check_layout() -> None:
    """L1-L3 — the properties that make the map usable, on a synthetic graph.

    Kept synthetic so this stays fast: the real layout is ~3 minutes, and these are
    properties of the algorithm, not of the data.

    L2 is the HALO regression — the bug this project has re-encountered most — so it
    is guarded twice, at the mechanism AND at the symptom. It used to be guarded at
    neither: the old L2 ran a 151-cell ring at 60 iterations, in which the halo is
    structurally INEXPRESSIBLE (every cell sat in a degree>=2 ring, isolated cells
    never enter the sim, and n=151 is orders of magnitude below where the halo
    equilibrium bites). Measured: setting `REPULSION_RANGE = 1e9` — i.e. deleting the
    fix and reverting to textbook long-range Fruchterman-Reingold — left all 7 checks
    GREEN, so the nightly would have built, gated and published a halo'd map.
    """
    import math

    import layout
    from layout import SPAN, layout_cells

    def fixture() -> dict:
        # a connected core + isolated cells (the halo bait). The isolated set covers
        # BOTH place_isolated branches: cells with a supercell (parked at its centroid)
        # and cells with none (the outer band). An all-homeless fixture left the
        # centroid branch — the one that actually runs on live data — untested.
        cells = {f"cell:c{i}": {"id": f"cell:c{i}", "organs": [],
                                "supercells": ["path:X" if i % 2 else "path:Y"]}
                 for i in range(120)}
        cells.update({f"cell:iso{i}": {"id": f"cell:iso{i}", "organs": [],
                                       "supercells": ["path:X"]} for i in range(20)})
        cells.update({f"cell:lone{i}": {"id": f"cell:lone{i}", "organs": []}
                      for i in range(10)})   # no supercell -> homeless band
        cells["cell:orphan"] = {"id": "cell:orphan", "organs": [],
                                "supercells": ["path:Z"]}  # supercell has no connected cell
        # The exact shape that collided on live data (20 cells): a supercell with
        # exactly ONE connected member, so its centroid IS that member's position,
        # plus a single isolated member that would land right on top of it.
        cells["cell:solo"] = {"id": "cell:solo", "organs": [], "supercells": ["path:S"]}
        cells["cell:solo_iso"] = {"id": "cell:solo_iso", "organs": [],
                                  "supercells": ["path:S"]}
        return cells

    synapses = [{"src": f"cell:c{i}", "dst": f"cell:c{(i + 1) % 120}", "weight": 3}
                for i in range(120)]
    synapses += [{"src": "cell:c0", "dst": f"cell:c{i}", "weight": 1}
                 for i in range(2, 40)]  # a hub

    cells = fixture()
    layout_cells(cells, synapses, iterations=60)
    first = {k: tuple(v["xy"]) for k, v in cells.items()}

    check("L1 supercell-anchored isolated cells are placed",
          all(f"cell:iso{i}" in first for i in range(20)))
    check("L1 a cell whose supercell has no connected member still gets an xy",
          "cell:orphan" in first and all(isinstance(v, float)
                                         for v in first["cell:orphan"]))
    coords = list(first.values())
    check("L1 no two cells land on the exact same point",
          len(set(coords)) == len(coords),
          f"{len(coords) - len(set(coords))} exact collisions")

    check("L1 every cell gets an xy", all("xy" in c for c in cells.values()))

    radii = sorted(math.hypot(*c["xy"]) for c in cells.values())
    median = radii[len(radii) // 2] or 1.0
    spread = radii[-1] / median
    check("L2 no halo: max radius stays within 8x the median", spread < 8.0,
          f"spread={spread:.1f}x — long-range repulsion is back")

    iso_r = [math.hypot(*cells[k]["xy"]) for k in cells if ":iso" in k or ":lone" in k]
    check("L2 isolated cells sit near the core, not in orbit",
          max(iso_r) < median * 8.0, f"isolated max radius={max(iso_r):.0f}")

    # ---- L2a: the MECHANISM. Probe the force law directly.
    #
    # Repulsion MUST be zero beyond REPULSION_RANGE*k, or a weakly-held cell is
    # pushed out until n*k^2/r balances gravity g*r — the halo (SCHEMA "Layout is
    # BUILD-TIME, and repulsion must be short-range").
    #
    # PROBE_GAP is deliberately an ABSOLUTE distance and NOT derived from
    # REPULSION_RANGE: deriving it would make this test self-defeating, since
    # REPULSION_RANGE=1e9 would push the probe out to 1e11 and it would report "still
    # beyond the range" and pass. 6k is a separation the live map is full of — its
    # MEDIAN cell radius alone is ~8.6k and its max ~32k — so "two unbonded cells 6k
    # apart must not shove each other" is a property of the shipped map, not of a
    # constant. Both directions are pinned: a range of 0 (repulsion deleted outright)
    # collapses the map onto the origin and must fail just as loudly.
    PROBE_GAP = 6.0 * SPAN
    try:
        import numpy as np

        def probe(half: float) -> float:
            """One step on two unbonded nodes at (+-half, 0); returns the new |x|."""
            empty = np.asarray([], dtype=np.int64)
            out = layout._simulate(np.array([[-half, 0.0], [half, 0.0]]),
                                   empty, empty, np.asarray([]),
                                   iterations=1, k=SPAN)
            return abs(float(out[1][0]))

        far = probe(PROBE_GAP / 2.0)
        near = probe(SPAN / 2.0)
        check("L2a repulsion is OFF beyond the cutoff (unbonded cells 6k apart are "
              "pulled together, not shoved apart)",
              far < PROBE_GAP / 2.0,
              f"a pair {PROBE_GAP:.0f} apart moved OUT to {far * 2:.0f} — repulsion is "
              f"long-range, the halo is back (REPULSION_RANGE={getattr(layout, 'REPULSION_RANGE', None)!r})")
        check("L2a repulsion is ON inside the cutoff (cells k apart still separate)",
              near > SPAN / 2.0,
              f"a pair {SPAN:.0f} apart collapsed to {near * 2:.0f} — repulsion is gone "
              f"entirely, the map piles on the origin")
    except Exception as exc:   # a deleted REPULSION_RANGE raises NameError in _simulate
        check("L2a the repulsion cutoff exists and is probeable", False,
              f"{type(exc).__name__}: {exc}")

    # ---- L2b: the SYMPTOM. A halo, on a fixture that can actually express one.
    #
    # The victim shape is a component with nothing holding it IN: edge attraction
    # grows as d^2, so a bonded cell is always reeled back, but a DETACHED component
    # only feels repulsion out and gravity in — it settles at r = sqrt(n*k^2/g),
    # exactly the halo formula. (The old fixture had no such component, which is why
    # it could not see the bug.) 200 iterations because the step is temperature-capped
    # and the halo is ~8.7k away: at 60 the sim cannot physically reach equilibrium
    # and the fixture reports a false GREEN.
    #
    # Asserted against the PREDICTED halo radius rather than a max/median ratio,
    # because long-range repulsion inflates the core too: measured, the ratio only
    # reaches 3.1x (median rises with it) and would slip under any threshold loose
    # enough to be stable — the absolute radius is what actually separates the two
    # regimes (fix ON r=1,366 = 0.16x predicted; fix OFF r=8,505 = 0.98x predicted).
    n_core = 150
    halo = {f"cell:h{i}": {"id": f"cell:h{i}", "organs": [], "supercells": ["path:X"]}
            for i in range(n_core)}
    halo["cell:far0"] = {"id": "cell:far0", "organs": []}
    halo["cell:far1"] = {"id": "cell:far1", "organs": []}
    halo_syn = [{"src": f"cell:h{i}", "dst": f"cell:h{(i + 1) % n_core}", "weight": 3}
                for i in range(n_core)]
    halo_syn += [{"src": "cell:far0", "dst": "cell:far1", "weight": 1}]  # detached pair
    layout_cells(halo, halo_syn, iterations=200)
    core_r = sorted(math.hypot(*halo[f"cell:h{i}"]["xy"]) for i in range(n_core))
    core_med = core_r[len(core_r) // 2] or 1.0
    far_r = math.hypot(*halo["cell:far0"]["xy"])
    predicted = math.sqrt(len(halo) * SPAN ** 2 / layout.GRAVITY)   # the halo formula
    check("L2b a detached component settles near the core, nowhere near the "
          "long-range equilibrium radius",
          far_r < predicted * 0.5,
          f"detached component at r={far_r:.0f}, which is {far_r / predicted:.0%} of the "
          f"predicted halo radius sqrt(n*k^2/g)={predicted:.0f} (core median {core_med:.0f}) "
          f"— it is orbiting, not settling")

    # L3 — determinism. The map must be the same every rebuild, or it cannot be
    # learned (the whole reason layout moved to build time).
    cells2 = fixture()
    layout_cells(cells2, synapses, iterations=60)
    second = {k: tuple(v["xy"]) for k, v in cells2.items()}
    check("L3 layout is deterministic across rebuilds", first == second,
          "same inputs produced a different map")


def main() -> int:
    print("building cell layer (no layout)…")
    cells_list, synapses, meta, review = build(do_layout=False)
    cells = {c["id"]: c for c in cells_list}
    print(f"\n{len(cells)} cells / {len(synapses)} synapses\n")

    # ---- C1: the Module atom (Jack's example — Module and Vector space are ONE atom)
    module = cells.get("cell:Q18848")
    if module is None:
        check("C1 Module atom exists", False, "cell:Q18848 absent")
    else:
        ids = organ_ids(module)
        for want in ("Q18848", "Q125977", "decl:Mathlib:Module"):
            check(f"C1 Module atom holds {want}", want in ids,
                  f"organs={sorted(ids)}")

    # ---- C2: anti-chaining. Euclidean space has its OWN exact decl, so it must not
    # be absorbed into the Module atom (a transitive closure fuses them via
    # Vector space; that is the bug this whole model exists to prevent).
    euclid = cells.get("cell:Q17295")
    check("C2 Euclidean space is its own cell", euclid is not None,
          "cell:Q17295 absent — it was absorbed, the merge chained")
    if euclid and module:
        check("C2 Euclidean space is NOT the Module atom",
              euclid["id"] != module["id"])
        check("C2 EuclideanSpace decl is not in the Module atom",
              "decl:Mathlib:EuclideanSpace" not in organ_ids(module))
        check("C2 Module decl is not in the Euclidean atom",
              "decl:Mathlib:Module" not in organ_ids(euclid))

    # ---- C3: no blob. Rule 2 attaching to a SINGLE best target bounds cell size;
    # a blob means chaining crept back in.
    biggest = max(cells.values(), key=lambda c: len(c["organs"]))
    check(f"C3 no cell exceeds {CELL_MAX_ORGANS} organs",
          len(biggest["organs"]) <= CELL_MAX_ORGANS,
          f"{biggest['id']} has {len(biggest['organs'])}")

    # ---- C4: every organ resolves to exactly ONE cell (aliases.json must be a
    # function, or /brain#Q181296 and every API route become ambiguous).
    seen: Counter = Counter()
    for cell in cells.values():
        for oid in organ_ids(cell):
            seen[oid] += 1
    dupes = [o for o, n in seen.items() if n > 1]
    check("C4 every organ resolves to exactly one cell", not dupes,
          f"{len(dupes)} organs in >1 cell, e.g. {dupes[:5]}")

    # ---- C5: pages never bridge. A page claimed by >1 cell must live on a
    # supercell; if it sat on a cell it would silently fuse unrelated atoms.
    supercell_organs = meta["supercell_organs"]
    area_pages = {o["id"] for organs in supercell_organs.values() for o in organs
                  if o["kind"] == "page"}
    leaked = [p for p in area_pages if any(p in organ_ids(c, "page") for c in cells.values())]
    check("C5 area pages live on supercells, not cells", not leaked,
          f"leaked onto cells: {leaked[:5]}")
    check("C5 supercells carry field concepts",
          any(o["kind"] == "concept" for organs in supercell_organs.values()
              for o in organs),
          "no field concept reached a supercell")
    # Jack's example: "Linear algebra" belongs to the folder, not to the Module atom.
    la = [p for p, organs in supercell_organs.items()
          if any(o["id"] == "Q82571" for o in organs)]
    check("C5 'Linear algebra' (Q82571) is a supercell organ", bool(la),
          "Q82571 did not land on a supercell")
    if module:
        check("C5 'Linear algebra' is NOT in the Module atom",
              "Q82571" not in organ_ids(module))
    # Rule 5 says "never a cell", not merely "not in that cell": a field concept whose
    # only home is a supercell must not also become a lone-particle atom, or searching
    # "linear algebra" lands on a stray cell instead of the LinearAlgebra folder.
    check("C5 'Linear algebra' is not a cell of its own",
          "cell:Q82571" not in cells,
          "field concept became a lone-particle cell — rule 5 says never a cell")
    field_cells = [p for p, organs in supercell_organs.items() for o in organs
                   if o["kind"] == "concept" and f"cell:{o['id']}" in cells
                   and len(cells[f"cell:{o['id']}"]["organs"]) == 1]
    check("C5 no field concept is a lone-particle cell", not field_cells,
          f"{len(field_cells)} field concepts also became cells, e.g. {field_cells[:3]}")

    # ---- C6: synapse hygiene
    pairs = Counter((s["src"], s["dst"]) for s in synapses)
    dup_pairs = [p for p, n in pairs.items() if n > 1]
    check("C6 no synapse duplicates a cell pair", not dup_pairs,
          f"{len(dup_pairs)} duplicated, e.g. {dup_pairs[:3]}")
    traceless = [s for s in synapses if not s.get("traces")]
    check("C6 every synapse carries >=1 trace", not traceless,
          f"{len(traceless)} traceless, e.g. {[s['src'] for s in traceless[:3]]}")
    self_loops = [s for s in synapses if s["src"] == s["dst"]]
    check("C6 no self-loop synapses", not self_loops, f"{len(self_loops)} self-loops")
    valid = set(cells) | set(supercell_organs)
    unresolved = [s for s in synapses if s["src"] not in valid or s["dst"] not in valid]
    check("C6 every synapse endpoint is a real cell or supercell", not unresolved,
          f"{len(unresolved)} dangling, e.g. {[s['src'] for s in unresolved[:3]]}")
    # A rule-5 field concept owns no cell, but it is a HUB — dropping its bonds
    # instead of routing them to its supercell cost 10,801 synapses (12% of the graph).
    sup_syn = [s for s in synapses
               if s["src"].startswith("path:") or s["dst"].startswith("path:")]
    check("C6 rule-5 field concepts keep their bonds (routed to the supercell)",
          len(sup_syn) > 1000, f"only {len(sup_syn)} supercell synapses — hubs dropped?")

    # ---- C7: tag queue. A rejected claim must never bond, and a queued (AI) tag
    # must stay distinguishable from a merged @[wikidata] attribute.
    prov = meta["prov"]
    queued_prov = {i for i, p in enumerate(prov) if p.get("source") == "tag-queue"}
    merged_prov = {i for i, p in enumerate(prov)
                   if "@[wikidata] attribute" in (p.get("method") or "")}
    check("C7 queued tags are provenance-distinguishable",
          bool(queued_prov) and bool(merged_prov) and not (queued_prov & merged_prov),
          f"queued={len(queued_prov)} merged={len(merged_prov)}")
    check("C7 the rejected-claim guard was exercised at all",
          meta["stats"].get("queue_rejected", 0) > 0,
          "no rejected claim was exercised — the guard is untested")
    # The counter above only proves the guard RAN; it would stay green while a
    # rejected claim leaked through, so assert what the contract actually says: no
    # rejected (qid, decl) pair may be fused BY THE QUEUE. Deliberately not "may not
    # share a cell" — 8 rejected pairs legitimately do, fused by a merged
    # @[wikidata]/oracle grounding, which rule 1 permits. The rejection binds the
    # QUEUE claim, not the pair.
    owner = {o["id"]: c["id"] for c in cells.values() for o in c["organs"]}
    leaked = []
    for qid, decl in load_rejected():
        did = f"decl:Mathlib:{decl}"
        if not owner.get(qid) or owner.get(qid) != owner.get(did):
            continue
        for organ in cells[owner[qid]]["organs"]:
            if (organ["id"] in (qid, did) and "prov" in organ
                    and prov[organ["prov"]].get("source") == "tag-queue"):
                leaked.append((qid, decl))
    check("C7 no rejected queue claim fused its pair (the guard WORKS, not just ran)",
          not leaked,
          f"{len(leaked)} rejected pairs bonded via the queue, e.g. {leaked[:3]}")
    # Jack's example: the zeta atom should hold BOTH zeta decls (the completion's
    # tag is `revise`, not rejected, so the queue bond is legitimate).
    zeta = cells.get("cell:Q187235")
    if zeta:
        ids = organ_ids(zeta, "decl")
        check("C7 zeta atom holds riemannZeta AND completedRiemannZeta",
              {"decl:Mathlib:riemannZeta", "decl:Mathlib:completedRiemannZeta"} <= ids,
              f"decls={sorted(ids)}")
        # Q187235 is fused by BOTH a merged @[wikidata] attribute (-> riemannZeta) and
        # an AI queue candidate (-> completedRiemannZeta). The organ must report the
        # MERGED provenance: last-write-wins let the queue bond overwrite it, which
        # inverts the very distinction C7 exists to guarantee.
        organ = next((o for o in zeta["organs"] if o["id"] == "Q187235"), None)
        src = prov[organ["prov"]].get("source") if organ and "prov" in organ else None
        check("C7 a merged @[wikidata] tag outranks an AI queue bond on the same organ",
              src != "tag-queue", f"Q187235 organ reports provenance source={src!r}")

    # ---- carried grounding fix (Jack: Module is not a generalization of *vector*)
    vector = cells.get("cell:Q13471665")
    check("Q13471665 'Vector' keeps its own cell (grounding override applied)",
          vector is not None and "decl:Mathlib:Module" not in organ_ids(vector),
          "Vector was absorbed into a decl atom — override not applied?")

    check_layout()

    print(f"\n{CHECKS - len(FAILURES)}/{CHECKS} checks passed")
    if FAILURES:
        print("\n\033[31mRED\033[0m — the cell contract is broken:")
        for f in FAILURES:
            print(f"  - {f}")
        return 1
    print("\033[32mGREEN\033[0m — cell contract holds")
    return 0


if __name__ == "__main__":
    sys.exit(main())
