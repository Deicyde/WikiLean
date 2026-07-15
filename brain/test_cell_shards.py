#!/usr/bin/env python3
"""Acceptance for the v3 cell SHARDS — S1..S6.

test_cells.py proves the atom layer (brain/data/*.jsonl). This proves the artifact
the browser and the Worker actually read: site/assets/brain/cells/. They can drift
independently — the shard builder trims, embeds and re-indexes — so the properties
are checked against the shipped bytes, not against the builder's intent.

  S1  the manifest's own lookup rule resolves every cell to a shard that holds it
  S2  aliases.json is a FUNCTION over organ ids, and resolves the v2 entry points
  S3  a cell entry is SELF-CONTAINED — one fetch renders the whole card
  S4  explorer.json indices are in range, and its omissions are accounted for
  S5  supercells.json is a consistent tree whose leaves are cells
  S6  no licensed snippet ships without its licence

Run: python3 brain/test_cell_shards.py   (after brain/build_cell_shards.py)
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
OUT = ROOT / "site" / "assets" / "brain" / "cells"

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


def main() -> int:
    if not OUT.exists():
        print(f"missing {OUT} — run python3 brain/build_cell_shards.py first")
        return 1
    manifest = json.loads((OUT / "manifest.json").read_text())
    aliases = json.loads((OUT / "aliases.json").read_text())
    explorer = json.loads((OUT / "explorer.json").read_text())
    supercells = json.loads((OUT / "supercells.json").read_text())
    labels = json.loads((OUT / "labels.json").read_text())
    scheme = manifest["scheme"]
    print(f"{manifest['_meta']['counts']['cells']} cells / "
          f"{len(manifest['shards'])} shards\n")

    def shard_key(nid: str, length: int) -> str:
        """The manifest's documented rule, reimplemented from the SPEC not the code —
        if the client's reading of `lookup` diverges from the builder, that is the
        bug this catches."""
        out = ""
        for i in range(length):
            c = nid[i].lower() if i < len(nid) else scheme["pad"]
            out += c if ("a" <= c <= "z" or "0" <= c <= "9") else scheme["pad"]
        return out

    def resolve(cid: str) -> dict | None:
        keys = [k for k in manifest["shards"] if shard_key(cid, len(k)) == k]
        if not keys:
            return None
        blob = json.loads((OUT / f"{max(keys, key=len)}.json").read_text())
        return blob.get(cid)

    # ---- S1: every cell is exactly one fetch away
    all_ids = [n["id"] for n in explorer["nodes"]]
    sample = all_ids[::311] + ["cell:Q18848", "cell:Q187235", "cell:Q17295"]
    missing = [cid for cid in sample if resolve(cid) is None]
    check("S1 the manifest lookup rule resolves every sampled cell", not missing,
          f"unresolvable: {missing[:5]}")
    prefix_free = [k for k in manifest["shards"]
                   for j in manifest["shards"] if j != k and k.startswith(j)]
    check("S1 shard keys are prefix-free", not prefix_free,
          f"{len(prefix_free)} keys shadow another, e.g. {prefix_free[:3]}")

    # ---- S2: aliases is the compat layer. It MUST be a function (C4) and must
    # resolve the entry points v2 exposed, or /brain#Q..., the API and MCP 404.
    organs = aliases["organs"]
    # "Exactly one owner" is free — a JSON object cannot map a key to two values — so
    # asserting it proves nothing. The property with teeth is that the owner is a
    # STRING NAMING SOMETHING THAT EXISTS, checked against both owner kinds below.
    check("S2 aliases.json maps every organ id to a single named owner",
          all(isinstance(v, str) and v for v in organs.values()),
          f"{sum(1 for v in organs.values() if not isinstance(v, str) or not v)} bad values")
    check("S2 the alias count in _meta matches the rows shipped",
          aliases["_meta"]["counts"]["organs"] == len(organs),
          f"_meta says {aliases['_meta']['counts']['organs']}, shipped {len(organs)}")
    for organ, want in [("Q125977", "cell:Q18848"),        # Vector space -> Module atom
                        ("decl:Mathlib:Module", "cell:Q18848"),
                        ("Vector_space", "cell:Q18848"),   # the article slug
                        ("Q82571", "path:Mathlib/LinearAlgebra")]:  # rule 5
        check(f"S2 {organ} resolves to {want}", organs.get(organ) == want,
              f"got {organs.get(organ)!r}")
    dangling = [o for o, owner in organs.items()
                if owner.startswith("cell:") and resolve(owner) is None]
    check("S2 every organ's owning cell exists as a shard entry", not dangling,
          f"{len(organs)} organs checked; {len(dangling)} point at a missing cell, "
          f"e.g. {dangling[:3]}")
    # The other half of the alias space: a rule-5 organ ("Linear algebra") resolves to
    # a SUPERCELL, not a cell. Only `cell:` owners were validated, so a path: owner
    # naming a supercell that does not exist resolved to nothing and nobody noticed —
    # the same 404 the cell check exists to prevent, on the minority branch.
    sup_tree = supercells["supercells"]
    bad_sup = sorted({owner for owner in organs.values()
                      if owner.startswith("path:") and owner not in sup_tree})
    check("S2 every organ's owning SUPERCELL exists in supercells.json", not bad_sup,
          f"{len(bad_sup)} rule-5 organs point at a missing supercell, e.g. {bad_sup[:3]}")
    stray = sorted({owner for owner in organs.values()
                    if not owner.startswith(("cell:", "path:"))})
    check("S2 every owner is a cell or a supercell", not stray,
          f"unknown owner kinds: {stray[:3]}")

    # ---- S3: one fetch renders the card. If an organ's payload is missing the card
    # has to fan out per organ, which is the locality law this whole scheme exists
    # to satisfy.
    entry = resolve("cell:Q18848")
    if entry is None:
        check("S3 the Module atom resolves", False, "cell:Q18848 absent")
    else:
        kinds = {o["kind"] for o in entry["organs"]}
        check("S3 the Module atom's card carries every organ kind in one fetch",
              {"concept", "decl", "page", "article"} <= kinds, f"kinds={sorted(kinds)}")
        decl = next(o for o in entry["organs"] if o["kind"] == "decl")
        check("S3 a decl organ embeds its Lean code", bool(decl.get("code")))
        concept = next(o for o in entry["organs"]
                       if o["kind"] == "concept" and o["id"] == "Q18848")
        check("S3 a concept organ embeds its Wikidata description",
              bool(concept.get("description")))
        check("S3 the card embeds its breadcrumb + synapses",
              bool(entry.get("breadcrumb")) and bool(entry.get("syn")))
        check("S3 synapses carry evidence traces",
              any(s.get("traces") for s in entry["syn"]))
        # The trace cap must sample DIVERSELY. `depends` outnumbers everything ~10:1,
        # so taking the first N buries the rare cross-database `links` trace — the one
        # that names both pages — behind bulk formal-dependency traces.
        mixed = [s for s in entry["syn"] if len(s.get("kinds") or {}) > 1
                 and (s.get("tt") or 0) > len(s.get("traces") or [])]
        bad = [s for s in mixed
               if {t["kind"] for t in s["traces"]} < set(s["kinds"])
               and len(s["traces"]) >= len(s["kinds"])]
        check("S3 a capped synapse shows every bond KIND it claims", not bad,
              f"{len(bad)}/{len(mixed)} capped synapses hide a whole kind, "
              f"e.g. {[(s['id'], list(s['kinds']), sorted({t['kind'] for t in s['traces']})) for s in bad[:2]]}")

    # ---- S4: the explorer. v2's 4,000-edge draw cap pointed edges at nodes that
    # were never shipped — the phantom-ring bug. Indices make that a hard error.
    n = len(explorer["nodes"])
    bad = [e for e in explorer["edges"] if not (0 <= e[0] < n and 0 <= e[1] < n)]
    check("S4 every explorer edge indexes a shipped node", not bad,
          f"{len(bad)} out-of-range")
    check("S4 every explorer node carries a build-time xy",
          all(len(x.get("xy") or []) == 2 for x in explorer["nodes"]))

    # "Nothing is truncated" must be MEASURED against the shipped arrays, never read
    # back from the flag. The builder writes `"truncated": False` unconditionally and
    # its byte-budget guard only prints a warning, so no code path can set it True:
    # the old check asserted a literal and could not fail — if a future change ever
    # did truncate the explorer, it would have stayed green and the phantom-ring bug
    # would return under a green suite. So count the actual rows instead, and only
    # then require the flag to agree with the measurement.
    counts = explorer["_meta"]["counts"]
    n_edges, n_nodes = len(explorer["edges"]), len(explorer["nodes"])
    check("S4 the explorer's arrays match the counts it advertises",
          n_edges == counts["edges"] and n_nodes == counts["nodes"],
          f"shipped {n_nodes} nodes/{n_edges} edges, _meta claims "
          f"{counts['nodes']}/{counts['edges']}")
    check("S4 every cell is a shipped explorer node",
          n_nodes == manifest["_meta"]["counts"]["cells"],
          f"{n_nodes} nodes != {manifest['_meta']['counts']['cells']} cells")
    # The real property the `truncated` flag claims: every synapse in the graph is
    # either an explorer edge or one of the declared supercell edges — none silently
    # dropped to fit a budget.
    total = n_edges + counts["supercell_edges_on_supercells_json"]
    complete = total == manifest["_meta"]["counts"]["synapses"]
    check("S4 the shipped edges + the declared supercell split == every synapse "
          "(nothing truncated)", complete,
          f"{n_edges} shipped + {counts['supercell_edges_on_supercells_json']} supercell "
          f"= {total} != {manifest['_meta']['counts']['synapses']} synapses — "
          f"{manifest['_meta']['counts']['synapses'] - total} went missing")
    check("S4 the truncated flag agrees with the measured edge count",
          explorer["_meta"]["truncated"] is (not complete),
          f"_meta.truncated={explorer['_meta']['truncated']!r} but the arrays "
          f"{'reconcile' if complete else 'do NOT reconcile'}")
    # Independent of the counts entirely: a cell's shard says how many synapses it
    # has; the explorer must actually carry that many (minus its supercell bonds). A
    # builder that shipped a truncated edge array while writing honest counts would
    # pass every check above and fail this one.
    idx = {node["id"]: i for i, node in enumerate(explorer["nodes"])}
    degree: dict[int, int] = {}
    for e in explorer["edges"]:
        degree[e[0]] = degree.get(e[0], 0) + 1
        degree[e[1]] = degree.get(e[1], 0) + 1
    short, sampled = [], 0
    for cid in ["cell:Q18848", "cell:Q17295"] + all_ids[::97]:
        cell_entry = resolve(cid)
        # Only cells whose syn list is COMPLETE: on a capped cell the shipped rows
        # undercount the supercell bonds among the ones the cap dropped, so the
        # arithmetic below would not be exact.
        if not cell_entry or cell_entry.get("truncated"):
            continue
        sampled += 1
        to_super = sum(1 for s in cell_entry["syn"] if s["id"].startswith("path:"))
        # counts.syn is the cell's true synapse total; the explorer is uncapped, so
        # its degree must be exactly that total minus the cell's supercell bonds
        want = cell_entry["counts"]["syn"] - to_super
        got = degree.get(idx.get(cid, -1), 0)
        if got != want:
            short.append((cid, got, want))
    check(f"S4 each cell's explorer degree matches the synapse total its shard claims "
          f"({sampled} sampled)", not short,
          f"{len(short)} cells disagree (id, explorer_degree, shard_says), "
          f"e.g. {short[:3]}")

    # ---- S5: the containment tree the bubble view walks
    tree = supercells["supercells"]
    orphans = [p for p, r in tree.items() if r.get("parent") and r["parent"] not in tree]
    check("S5 every supercell's parent exists", not orphans,
          f"{len(orphans)} orphans, e.g. {orphans[:3]}")
    ids = set(all_ids)
    bad_cells = [c for r in tree.values() for c in (r.get("cells") or []) if c not in ids]
    check("S5 every supercell child cell exists", not bad_cells,
          f"{len(bad_cells)} dangling, e.g. {bad_cells[:3]}")
    check("S5 roots have no parent",
          all(not tree[r].get("parent") for r in supercells["roots"]))
    check("S5 supercells carry rule-5 organs",
          any(r.get("organs") for r in tree.values()))
    # Supercell synapses ship traceless on purpose (byte budget — this file is
    # fetched eagerly). That is only acceptable if the artifact SAYS so and names
    # where to get them, or a reader silently renders an empty evidence drawer.
    check("S5 the traceless-supercell-synapse omission is declared in _meta",
          "neighborhood" in (supercells["_meta"].get("traces") or ""),
          "supercell syn rows have no traces and _meta does not say where to fetch them")
    # A supercell has no tag bits of its own, so a facet chip would dim every folder
    # — "showing 0 of N" over a grey canvas (the v2 bug report). `fa` aggregates the
    # subtree's bits so folders survive the filter.
    root_fa = tree.get("path:Mathlib", {}).get("fa", 0)
    check("S5 supercells carry subtree-aggregate facet bits (fa)", root_fa > 0,
          "path:Mathlib has no fa — every facet chip would grey the canvas")
    leaked = [p for p, r in tree.items()
              if r.get("cells") and not r.get("fa")
              and any(c in ids and explorer["nodes"][all_ids.index(c)].get("f")
                      for c in r["cells"][:3])]
    check("S5 a supercell holding a faceted cell has fa", not leaked,
          f"{len(leaked)} folders would dim wrongly, e.g. {leaked[:3]}")

    # ---- S6: licensing. Snippets exist only for permitting sources and must carry
    # their per-source licence wherever they are rendered.
    unlicensed = []
    for key in list(manifest["shards"])[:40]:
        for cell in json.loads((OUT / f"{key}.json").read_text()).values():
            for organ in cell["organs"]:
                if organ.get("snippet") and not organ.get("snippet_license"):
                    unlicensed.append(organ["id"])
    check("S6 no snippet ships without its licence", not unlicensed,
          f"{len(unlicensed)} unlicensed, e.g. {unlicensed[:3]}")

    # ---- search
    check("labels.json lets an organ label find its atom",
          any(r["id"] == "cell:Q18848" and "Vector space" in (r.get("aka") or [])
              for r in labels),
          "searching 'Vector space' would not surface the Module atom")

    print(f"\n{CHECKS - len(FAILURES)}/{CHECKS} checks passed")
    if FAILURES:
        print("\n\033[31mRED\033[0m — the shard contract is broken:")
        for f in FAILURES:
            print(f"  - {f}")
        return 1
    print("\033[32mGREEN\033[0m — shard contract holds")
    return 0


if __name__ == "__main__":
    sys.exit(main())
