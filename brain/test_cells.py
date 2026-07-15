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

from build_cells import ATTACH, CELL_MAX_ORGANS, FUSE, build  # noqa: E402

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
    """
    import math

    from layout import layout_cells

    # a connected core + deliberately isolated cells (the halo bait)
    cells = {f"cell:c{i}": {"id": f"cell:c{i}", "organs": [],
                            "supercells": ["path:X"]} for i in range(120)}
    cells.update({f"cell:iso{i}": {"id": f"cell:iso{i}", "organs": []}
                  for i in range(30)})
    synapses = [{"src": f"cell:c{i}", "dst": f"cell:c{(i + 1) % 120}", "weight": 3}
                for i in range(120)]
    synapses += [{"src": "cell:c0", "dst": f"cell:c{i}", "weight": 1}
                 for i in range(2, 40)]  # a hub

    layout_cells(cells, synapses, iterations=60)
    first = {k: tuple(v["xy"]) for k, v in cells.items()}

    check("L1 every cell gets an xy", all("xy" in c for c in cells.values()))

    # L2 — the halo regression. Long-range repulsion parked isolated cells at
    # r~84,200 while the real graph sat at r~1,985 (a 42x spread; fit-to-content
    # then rendered the graph as a dot inside a ring). Isolated cells must land
    # near the core, not in orbit.
    radii = sorted(math.hypot(*c["xy"]) for c in cells.values())
    median = radii[len(radii) // 2] or 1.0
    spread = radii[-1] / median
    check("L2 no halo: max radius stays within 8x the median", spread < 8.0,
          f"spread={spread:.1f}x — long-range repulsion is back")

    iso_r = [math.hypot(*cells[f"cell:iso{i}"]["xy"]) for i in range(30)]
    check("L2 isolated cells sit near the core, not in orbit",
          max(iso_r) < median * 8.0, f"isolated max radius={max(iso_r):.0f}")

    # L3 — determinism. The map must be the same every rebuild, or it cannot be
    # learned (the whole reason layout moved to build time).
    cells2 = {f"cell:c{i}": {"id": f"cell:c{i}", "organs": [],
                             "supercells": ["path:X"]} for i in range(120)}
    cells2.update({f"cell:iso{i}": {"id": f"cell:iso{i}", "organs": []}
                   for i in range(30)})
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
    unresolved = [s for s in synapses if s["src"] not in cells or s["dst"] not in cells]
    check("C6 every synapse endpoint is a real cell", not unresolved,
          f"{len(unresolved)} dangling")

    # ---- C7: tag queue. A rejected claim must never bond, and a queued (AI) tag
    # must stay distinguishable from a merged @[wikidata] attribute.
    prov = meta["prov"]
    queued_prov = {i for i, p in enumerate(prov) if p.get("source") == "tag-queue"}
    merged_prov = {i for i, p in enumerate(prov)
                   if "@[wikidata] attribute" in (p.get("method") or "")}
    check("C7 queued tags are provenance-distinguishable",
          bool(queued_prov) and bool(merged_prov) and not (queued_prov & merged_prov),
          f"queued={len(queued_prov)} merged={len(merged_prov)}")
    check("C7 rejected queue claims never bond",
          meta["stats"].get("queue_rejected", 0) > 0,
          "no rejected claim was exercised — the guard is untested")
    # Jack's example: the zeta atom should hold BOTH zeta decls (the completion's
    # tag is `revise`, not rejected, so the queue bond is legitimate).
    zeta = cells.get("cell:Q187235")
    if zeta:
        ids = organ_ids(zeta, "decl")
        check("C7 zeta atom holds riemannZeta AND completedRiemannZeta",
              {"decl:Mathlib:riemannZeta", "decl:Mathlib:completedRiemannZeta"} <= ids,
              f"decls={sorted(ids)}")

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
