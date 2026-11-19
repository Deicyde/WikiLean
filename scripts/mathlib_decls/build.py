"""Offline, deterministic build of the `wikilean.brain.mathlib-decls/v1` export.

Reads trough's `data/mathlib/` at its pinned commit (concept-deps.json, the curated yaml lists,
source.json) and `decls/mathlib.bmp` under --inputs, and writes names.jsonl, decls.jsonl,
deps.jsonl, rejected.jsonl and manifest.json under --out. Same inputs, same bytes.
Contract: docs/BRAIN-PRODUCERS.md.
"""

from __future__ import annotations

import json

from scripts.brain_producers import decls as D

from . import lists as L

SCHEMA = "wikilean.brain.mathlib-decls/v1"
PRODUCER = {"name": "scripts.mathlib_decls", "version": "1.0.0"}
LIBRARY = "Mathlib"
INDEX = ("mathlib_decls", "decls/mathlib.bmp")
TROUGH_FILES = ["concept-deps.json", "source.json"] + [f"yaml/{f}" for f in sorted(L.LISTS.values())]
MATHLIB_REPO = "leanprover-community/mathlib4"
# Module roots whose files live in the mathlib4 repository (others, like Init, do not).
REPOS = {"Mathlib": MATHLIB_REPO, "Archive": MATHLIB_REPO, "Counterexamples": MATHLIB_REPO}
FILES = D.FILES
ATTRIBUTION = [
    {"work": "Mathlib", "url": "https://github.com/leanprover-community/mathlib4", "license": "Apache-2.0",
     "holder": "Mathlib contributors",
     "use": "declaration names, modules, kinds, line numbers, signatures and docstrings, copied as "
            "doc-gen4 and trough record them; which curated lists name a declaration (pointers only)"},
    {"work": "trough", "url": D.URLS["trough"], "license": "Apache-2.0",
     "holder": "Deicyde/trough contributors",
     "use": "which declarations are mapped and their dependencies; pointers only"},
]


def _json(files: dict, rel: str) -> dict:
    try:
        d = json.loads(files[rel])
    except ValueError as e:
        raise D.BuildError(f"trough: data/mathlib/{rel} is not JSON ({e})") from None
    if not isinstance(d, dict):
        raise D.BuildError(f"trough: data/mathlib/{rel} is not an object")
    return d


def build(inputs: str, out: str) -> dict:
    D.refuse_out_in_work_tree(out)
    pins = D.read_pins(inputs)
    tree, files = D.load_trough(inputs, pins, "mathlib", TROUGH_FILES)
    sha, index, index_modules = D.load_index(inputs, pins, *INDEX)
    cdeps, source = _json(files, "concept-deps.json"), _json(files, "source.json")
    concepts = cdeps.get("concepts") or {}
    cited = cdeps.get("cited") or {}
    rejected: list[dict] = []

    names, modules = D.name_rows(index, LIBRARY, rejected)

    mapped_by: dict[str, set[str]] = {}
    for src, group in (("concepts", concepts), ("cited", cited)):
        for n in group:
            mapped_by.setdefault(n, set()).add(src)
    for mod, info in sorted((cdeps.get("modules") or {}).items()):
        for n in (info.get("decls") or []) if isinstance(info, dict) else []:
            if isinstance(n, str):
                mapped_by.setdefault(n, set()).add("module_decls")
    for n in cdeps.get("missing") or []:
        rejected.append({"kind": "decl", "reason": "trough_missing", "candidate": n})
    for n in cdeps.get("unresolved") or []:
        rejected.append({"kind": "decl", "reason": "trough_unresolved", "candidate": n})

    # The curated lists: which list names each declaration, with a pointer per entry.
    on_lists: dict[str, list[dict]] = {}
    for lst, fname in sorted(L.LISTS.items()):
        rel = f"yaml/{fname}"
        path = f"data/mathlib/{rel}"
        for e in L.entries(lst, files[rel].decode("utf-8")):
            ptr = {"repo": D.TROUGH_REPO, "commit": tree.commit, "path": path, "line": e["line"]}
            what, val = L.classify(e["value"])
            base = {"kind": "list_entry", "list": lst, "entry": e["entry"], "evidence": ptr}
            if what == "reject":
                rejected.append({**base, "reason": val, "candidate": e["value"]})
            elif what == "module":
                why = "module_entry" if val in index_modules else "module_not_in_index"
                rejected.append({**base, "reason": why, "candidate": val})
            else:
                mapped_by.setdefault(val, set()).add("lists")
                on_lists.setdefault(val, []).append({"list": lst, "entry": e["entry"], **ptr})

    rev = source.get("rev") if isinstance(source.get("rev"), str) else None
    rows, ids = [], {}
    for n in sorted(mapped_by):
        if n not in modules:
            reason = "module_name" if n in index_modules else "not_in_index"
            rejected.append({"kind": "decl", "reason": reason, "candidate": n,
                             "mapped_by": sorted(mapped_by[n])})
            continue
        info = concepts.get(n) if isinstance(concepts.get(n), dict) else cited.get(n)
        entries = sorted(on_lists.get(n, []), key=lambda p: (p["list"], p["line"]))
        rows.append({"id": D.decl_id(LIBRARY, n), "name": n, "module": modules[n],
                     "kind": index[n].get("kind"),
                     **D.trough_fields(n, info, modules[n], rejected, repos=REPOS, commit=rev,
                                       extra=("deprecated",)),
                     "mapped_by": sorted(mapped_by[n]),
                     "lists": sorted({p["list"] for p in entries}),
                     "list_entries": entries})
        ids[n] = rows[-1]["id"]

    evidence = {"repo": D.TROUGH_REPO, "commit": tree.commit, "path": "data/mathlib/concept-deps.json"}
    edges = D.dep_edges(concepts, ids, evidence, rejected)

    docs = D.docs_commit(pins, INDEX[0])
    matches = None if docs is None or rev is None else rev == docs
    data = {"names.jsonl": D.jsonl_bytes(names), "decls.jsonl": D.jsonl_bytes(rows),
            "deps.jsonl": D.jsonl_bytes(edges), "rejected.jsonl": D.jsonl_bytes(D.rejected_rows(rejected))}
    counts = {
        "names": len(names),
        "decls": len(rows),
        "decls_by_mapped_by": D.count(rows, lambda r: "+".join(r["mapped_by"])),
        "decls_by_kind": D.count(rows, lambda r: str(r["kind"])),
        "decls_on_list": {lst: sum(1 for r in rows if lst in r["lists"]) for lst in sorted(L.LISTS)},
        "deps_by_kind": D.count(edges, lambda r: r["kind"]),
        "rejected_by_reason": D.count(rejected, lambda r: f"{r['kind']}/{r['reason']}"),
    }
    manifest = {
        "schema": SCHEMA,
        "producer": PRODUCER,
        "inputs": {
            "trough": D.trough_input(tree, pins, "mathlib", files, mathlib_repo=MATHLIB_REPO,
                                     mathlib_rev=source.get("rev"),
                                     concept_deps_mathlib_rev=cdeps.get("mathlib_rev")),
            INDEX[0]: D.index_input(pins, *INDEX, sha),
        },
        # The Mathlib commit trough analysed, and the one the docs were built from.
        "mathlib_commit": rev,
        "docs_commit": docs,
        "mathlib_commit_matches_docs": matches,
        "counts": counts,
        "attribution": ATTRIBUTION,
    }
    return D.seal(out, data, manifest)
