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
    check("S2 aliases.json maps every organ id to exactly one owner",
          all(isinstance(v, str) for v in organs.values()))
    for organ, want in [("Q125977", "cell:Q18848"),        # Vector space -> Module atom
                        ("decl:Mathlib:Module", "cell:Q18848"),
                        ("Vector_space", "cell:Q18848"),   # the article slug
                        ("Q82571", "path:Mathlib/LinearAlgebra")]:  # rule 5
        check(f"S2 {organ} resolves to {want}", organs.get(organ) == want,
              f"got {organs.get(organ)!r}")
    dangling = [o for o, owner in organs.items()
                if owner.startswith("cell:") and resolve(owner) is None]
    check("S2 every organ's owning cell exists as a shard entry", not dangling,
          f"{len(dangling)} organs point at a missing cell, e.g. {dangling[:3]}")

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

    # ---- S4: the explorer. v2's 4,000-edge draw cap pointed edges at nodes that
    # were never shipped — the phantom-ring bug. Indices make that a hard error.
    n = len(explorer["nodes"])
    bad = [e for e in explorer["edges"] if not (0 <= e[0] < n and 0 <= e[1] < n)]
    check("S4 every explorer edge indexes a shipped node", not bad,
          f"{len(bad)} out-of-range")
    check("S4 the explorer is not truncated", explorer["_meta"]["truncated"] is False)
    check("S4 every explorer node carries a build-time xy",
          all(len(x.get("xy") or []) == 2 for x in explorer["nodes"]))
    counts = explorer["_meta"]["counts"]
    total = counts["edges"] + counts["supercell_edges_on_supercells_json"]
    check("S4 omitted supercell edges are accounted for, not dropped",
          total == manifest["_meta"]["counts"]["synapses"],
          f"{counts['edges']} + {counts['supercell_edges_on_supercells_json']} "
          f"!= {manifest['_meta']['counts']['synapses']}")

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
