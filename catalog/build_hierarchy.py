#!/usr/bin/env python3
"""Build the BRAIN's hierarchy skeleton: the multi-library
library → module → subfolder → file → decl tree, deterministically, from
TheoremGraph's statement_formal.csv (388k decls across 39 Lean libraries).

This is level 1-3 (+ file/decl) of Jack's BRAIN, grounded in the real Lean
module structure — the scaffold that Wikidata concepts get placed onto (at their
right altitude) and that informal arXiv statements attach beside.

Output catalog/data/hierarchy.json (shape is a downstream contract — brain/SCHEMA.md;
all changes vs the v1 output are ADDITIVE):
  { libraries: { <lib>: {kind, n_decls, n_files, modules: {<mod>: {n_decls,
                 [n_direct], [superseded, superseded_note], sub:{…}}}} },
    subfields: [ {library, path, n_decls, [direct], [superseded]} … ],
    meta: {...} }
Run: python3 catalog/build_hierarchy.py
"""
from __future__ import annotations

import csv
import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
STMT = HERE / ".cache" / "statement_formal.csv"
OUT = HERE / "data" / "hierarchy.json"
MAX_DEPTH = 3           # library + this many module components kept nested everywhere
SPLIT_AT = 2000         # deeper than that, a node splits one more component while > this
csv.field_size_limit(10 ** 9)

# Every library in the snapshot, tagged math|cs|physics|tooling. Unknown new
# libraries default to "math" with a stderr warning so drift is loud, not silent.
LIBRARY_KIND = {
    # math — Mathlib + research formalization projects (mathematical content)
    "Mathlib": "math", "SpherePacking": "math", "Carleson": "math",
    "CombinatorialGames": "math", "PrimeNumberTheoremAnd": "math", "FLT": "math",
    "ClassFieldTheory": "math", "SphereEversion": "math", "PFR": "math",
    "BrownianMotion": "math", "APAP": "math", "FormalConjecturesForMathlib": "math",
    "PrimeCert": "math",        # Pocklington/Wieferich primality certificates: number theory
    "Toric": "math", "FltRegular": "math", "MiscYD": "math",
    "HarderNarasimhan": "math", "AddCombi": "math", "PersistentDecomp": "math",
    "LeanCamCombi": "math", "GibbsMeasure": "math", "ForbiddenMatrix": "math",
    "KolmogorovExtension4": "math",
    # cs — verified data structures / algorithms / complexity / numerics
    "Cslib": "cs",
    "Init": "cs", "Std": "cs", "Batteries": "cs",  # core verified data-structure libraries
    "LeanCert": "cs",           # verified interval-arithmetic / AD certificate engine
    "ChandraFurstLipton": "cs",  # number-on-forehead communication complexity
    # physics
    "Physlib": "physics",
    # tooling — non-math infra: compiler API, metaprogramming, build, tactics, widgets
    "Lean": "tooling",          # compiler/elaborator API, not a math library
    "Aesop": "tooling", "Qq": "tooling", "Lake": "tooling", "ImportGraph": "tooling",
    "ProofWidgets": "tooling", "LeanSearchClient": "tooling", "Plausible": "tooling",
    "Architect": "tooling",
}

# Snapshot-only modules superseded upstream — tombstoned in place, never deleted.
# Key = (library, component path under it); value = the note carried on the node.
SUPERSEDED = {
    ("Mathlib", ("Std",)):
        "deleted upstream: the Mathlib.Std shim no longer exists on mathlib4 master; "
        "present only in this TheoremGraph snapshot",
    ("Mathlib", ("SetTheory", "Game")):
        "migrated upstream to the standalone CombinatorialGames library; "
        "this snapshot predates the removal",
    ("Mathlib", ("SetTheory", "PGame")):
        "migrated upstream to the standalone CombinatorialGames library; "
        "this snapshot predates the removal",
    ("Mathlib", ("SetTheory", "Surreal")):
        "migrated upstream to the standalone CombinatorialGames library; "
        "this snapshot predates the removal",
    ("Mathlib", ("SetTheory", "Nimber")):
        "migrated upstream to the standalone CombinatorialGames library; "
        "this snapshot predates the removal",
}


def emit(trie: dict, depth: int) -> dict:
    """Trie node -> output node. Children are kept up to MAX_DEPTH everywhere;
    past that a node keeps splitting one component at a time while > SPLIT_AT
    (or until the module path is exhausted). When children are shown, decls
    living directly at this node (module == this exact path) are counted in
    n_direct rather than a synthetic child, so n_decls == n_direct + sum(children)
    and every subfield path stays a real module prefix."""
    node: dict = {"n_decls": trie["n"]}
    show = depth < MAX_DEPTH or trie["n"] > SPLIT_AT
    if show and trie["kids"]:
        if trie["direct"]:
            node["n_direct"] = trie["direct"]
        node["sub"] = {k: emit(v, depth + 1) for k, v in trie["kids"].items()}
    else:
        node["sub"] = {}
    return node


def collect_subfields(lib: str, node: dict, path: str, superseded: bool, out: list) -> None:
    superseded = superseded or node.get("superseded", False)
    if node["sub"]:
        if node.get("n_direct"):
            row = {"library": lib, "path": path, "n_decls": node["n_direct"], "direct": True}
            if superseded:
                row["superseded"] = True
            out.append(row)
        for k, v in node["sub"].items():
            collect_subfields(lib, v, f"{path}.{k}", superseded, out)
    else:  # every output-tree leaf is a bubble, so subfields partition all decls
        row = {"library": lib, "path": path, "n_decls": node["n_decls"]}
        if superseded:
            row["superseded"] = True
        out.append(row)


def tree_depth(node: dict) -> int:
    return 1 + max((tree_depth(v) for v in node["sub"].values()), default=0)


def main() -> int:
    if not STMT.exists():
        raise SystemExit(f"missing {STMT} — download math-graph statement_formal.csv first")

    tries: dict[str, dict] = {}
    lib_files: dict[str, set] = {}
    lib_decls: dict[str, int] = {}
    n_rows = 0
    with STMT.open(newline="") as fh:
        for r in csv.DictReader(fh):
            mod = r.get("module") or ""
            if not mod:
                continue
            n_rows += 1
            parts = mod.split(".")
            lib = parts[0]
            lib_decls[lib] = lib_decls.get(lib, 0) + 1
            fp = r.get("file_path")
            if fp:
                lib_files.setdefault(lib, set()).add(fp)
            node = tries.setdefault(lib, {"n": 0, "direct": 0, "kids": {}})
            for comp in parts[1:]:
                node = node["kids"].setdefault(comp, {"n": 0, "direct": 0, "kids": {}})
                node["n"] += 1
            node["direct"] += 1

    libs: dict[str, dict] = {}
    for lib, trie in tries.items():
        kind = LIBRARY_KIND.get(lib)
        if kind is None:
            kind = "math"
            print(f"WARNING: library {lib!r} not in LIBRARY_KIND allowlist — "
                  f"defaulting kind to 'math'", file=sys.stderr)
        libs[lib] = {
            "kind": kind,
            "n_decls": lib_decls[lib],
            "n_files": len(lib_files.get(lib, ())),
            "modules": {k: emit(v, 1) for k, v in trie["kids"].items()},
        }

    for (lib, comps), note in SUPERSEDED.items():
        node = libs.get(lib, {}).get("modules", {})
        for c in comps[:-1]:
            node = node.get(c, {}).get("sub", {})
        target = node.get(comps[-1]) if node else None
        if target is None:
            print(f"WARNING: superseded node {lib}.{'.'.join(comps)} not found in snapshot",
                  file=sys.stderr)
            continue
        target["superseded"] = True
        target["superseded_note"] = note

    subfields: list[dict] = []
    for lib, L in sorted(libs.items(), key=lambda kv: -kv[1]["n_decls"]):
        for m1, n1 in L["modules"].items():
            collect_subfields(lib, n1, f"{lib}.{m1}", False, subfields)
    subfields.sort(key=lambda s: -s["n_decls"])

    max_depth_eff = max(
        (1 + tree_depth(m) for L in libs.values() for m in L["modules"].values()), default=0)
    sha = hashlib.sha256()
    with STMT.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            sha.update(chunk)
    st = STMT.stat()

    out = {
        "meta": {
            "source": "uw-math-ai/math-graph statement_formal.csv (TheoremGraph)",
            # snapshot mtime, NOT build time — rebuilds of the same CSV are byte-identical
            "generated_at": datetime.fromtimestamp(st.st_mtime, tz=timezone.utc)
                            .isoformat(timespec="seconds"),
            "source_bytes": st.st_size,
            "source_sha256": sha.hexdigest()[:16],
            "n_libraries": len(libs), "n_decls": n_rows,
            "n_subfields": len(subfields), "max_depth": MAX_DEPTH,
            "max_depth_effective": max_depth_eff, "split_threshold": SPLIT_AT,
            "note": "BRAIN levels 1-3: library -> module -> subfield. Wikidata concepts "
                    "attach at their altitude; Lean decls are the leaves; arXiv informal "
                    "statements attach beside their matched decls. Nodes > split_threshold "
                    "split one more path component recursively; decls sitting directly on "
                    "a split node are its n_direct (and a direct:true subfield row).",
        },
        "libraries": {k: v for k, v in sorted(libs.items(), key=lambda kv: -kv[1]["n_decls"])},
        "subfields": subfields,
    }
    tmp = OUT.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(out, ensure_ascii=False))
    tmp.replace(OUT)

    kinds: dict[str, int] = {}
    for L in libs.values():
        kinds[L["kind"]] = kinds.get(L["kind"], 0) + 1
    oversized = [s for s in subfields if s["n_decls"] > SPLIT_AT]
    print(f"hierarchy: {len(libs)} libraries / {n_rows} decls / {len(subfields)} subfields "
          f"/ depth<={max_depth_eff} / {len(oversized)} leaves still >{SPLIT_AT} "
          f"({OUT.stat().st_size / 1024:.0f} KB)")
    print("  kinds: " + ", ".join(f"{k}={v}" for k, v in sorted(kinds.items())))
    for lib, L in sorted(libs.items(), key=lambda kv: -kv[1]["n_decls"])[:8]:
        print(f"  {lib:22} {L['n_decls']:>7} decls  {L['n_files']:>5} files  "
              f"{len(L['modules']):>4} top-modules  [{L['kind']}]")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
