#!/usr/bin/env python3
"""Build the BRAIN's hierarchy skeleton: the multi-library
library → module → subfolder → file → decl tree, deterministically, from
TheoremGraph's statement_formal.csv (388k decls across 25 Lean libraries).

This is level 1-3 (+ file/decl) of Jack's BRAIN, grounded in the real Lean
module structure — the scaffold that Wikidata concepts get placed onto (at their
right altitude) and that informal arXiv statements attach beside.

Output catalog/data/hierarchy.json:
  { libraries: { <lib>: {n_decls, n_files, modules: {<mod>: {n_decls, sub:{…}}}} },
    subfields: [ {library, path, n_decls} … ],   # level-3 nodes (the natural bubbles)
    meta: {...} }
Run: python3 catalog/build_hierarchy.py
"""
from __future__ import annotations

import csv
import json
from pathlib import Path

HERE = Path(__file__).resolve().parent
STMT = HERE / ".cache" / "statement_formal.csv"
OUT = HERE / "data" / "hierarchy.json"
MAX_DEPTH = 3           # library + this many module components kept nested
csv.field_size_limit(10 ** 9)


def main() -> int:
    if not STMT.exists():
        raise SystemExit(f"missing {STMT} — download math-graph statement_formal.csv first")

    libs: dict[str, dict] = {}
    files_seen: dict[str, set] = {}
    n_rows = 0
    with STMT.open(newline="") as fh:
        for r in csv.DictReader(fh):
            mod = r.get("module") or ""
            if not mod:
                continue
            n_rows += 1
            parts = mod.split(".")
            lib = parts[0]
            L = libs.setdefault(lib, {"n_decls": 0, "n_files": 0, "modules": {}})
            L["n_decls"] += 1
            fp = r.get("file_path")
            if fp:
                files_seen.setdefault(lib, set()).add(fp)
            # nest the next MAX_DEPTH-1 module components under the library
            node = L["modules"]
            for comp in parts[1:MAX_DEPTH]:
                node = node.setdefault(comp, {"n_decls": 0, "sub": {}})
                node["n_decls"] += 1
                node = node["sub"]

    for lib, L in libs.items():
        L["n_files"] = len(files_seen.get(lib, ()))

    # Level-3 "subfield" nodes = library.moduleA.moduleB, the natural bubble grain.
    subfields = []
    for lib, L in sorted(libs.items(), key=lambda kv: -kv[1]["n_decls"]):
        for m1, n1 in L["modules"].items():
            if not n1["sub"]:
                subfields.append({"library": lib, "path": f"{lib}.{m1}", "n_decls": n1["n_decls"]})
            for m2, n2 in n1["sub"].items():
                subfields.append({"library": lib, "path": f"{lib}.{m1}.{m2}", "n_decls": n2["n_decls"]})
    subfields.sort(key=lambda s: -s["n_decls"])

    out = {
        "meta": {
            "source": "uw-math-ai/math-graph statement_formal.csv (TheoremGraph)",
            "n_libraries": len(libs), "n_decls": n_rows,
            "n_subfields": len(subfields), "max_depth": MAX_DEPTH,
            "note": "BRAIN levels 1-3: library -> module -> subfield. Wikidata concepts "
                    "attach at their altitude; Lean decls are the leaves; arXiv informal "
                    "statements attach beside their matched decls.",
        },
        "libraries": {k: v for k, v in sorted(libs.items(), key=lambda kv: -kv[1]["n_decls"])},
        "subfields": subfields,
    }
    OUT.write_text(json.dumps(out, ensure_ascii=False))
    top = sorted(libs.items(), key=lambda kv: -kv[1]["n_decls"])[:8]
    print(f"hierarchy: {len(libs)} libraries / {n_rows} decls / {len(subfields)} subfields "
          f"({OUT.stat().st_size / 1024:.0f} KB)")
    for lib, L in top:
        print(f"  {lib:22} {L['n_decls']:>7} decls  {L['n_files']:>5} files  "
              f"{len(L['modules']):>4} top-modules")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
