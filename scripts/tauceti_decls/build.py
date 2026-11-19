"""Offline, deterministic build of the `wikilean.brain.tauceti-decls/v1` export.

Reads trough's `data/tauceti/` at its pinned commit and `decls/tauceti.bmp` under --inputs
(the layout producer 1 uses) and writes names.jsonl, decls.jsonl, deps.jsonl, rejected.jsonl
and manifest.json under --out. Same inputs, same bytes. Contract: docs/BRAIN-PRODUCERS.md.
"""

from __future__ import annotations

import json

from scripts.brain_producers import decls as D

SCHEMA = "wikilean.brain.tauceti-decls/v1"
PRODUCER = {"name": "scripts.tauceti_decls", "version": "1.0.0"}
LIBRARY = "TauCeti"
INDEX = ("tauceti_decls", "decls/tauceti.bmp")
TROUGH_FILES = ["deps.json", "selection.json", "roadmaps.json", "source.json"]
REPOS = {"TauCeti": "TauCetiProject/TauCeti"}
FILES = D.FILES
ATTRIBUTION = [
    {"work": "TauCeti", "url": "https://github.com/TauCetiProject/TauCeti", "license": "Apache-2.0",
     "holder": "TauCeti contributors",
     "use": "declaration names, modules, kinds, line numbers, signatures and docstrings, copied as "
            "doc-gen4 and trough record them"},
    {"work": "trough", "url": D.URLS["trough"], "license": "Apache-2.0",
     "holder": "Deicyde/trough contributors",
     "use": "which declarations are mapped, their dependencies and sorry flags; pointers only"},
]


def _json(files: dict, rel: str) -> dict:
    try:
        d = json.loads(files[rel])
    except ValueError as e:
        raise D.BuildError(f"trough: data/tauceti/{rel} is not JSON ({e})") from None
    if not isinstance(d, dict):
        raise D.BuildError(f"trough: data/tauceti/{rel} is not an object")
    return d


def stated_reason(source: dict, deps: dict, docs: str | None) -> str | None:
    """None when trough's deps.json describes the Tau Ceti commit the docs were built from
    (producer 1's rule, plus deps.json's own `tauceti_rev` agreeing with source.json)."""
    if docs is None:
        return "docs_commit_unknown"
    if source.get("rev") != docs:
        return "commit_mismatch"
    if deps.get("tauceti_rev") not in (None, source.get("rev")):
        return "deps_rev_mismatch"
    return None


def build(inputs: str, out: str) -> dict:
    D.refuse_out_in_work_tree(out)
    pins = D.read_pins(inputs)
    tree, files = D.load_trough(inputs, pins, "tauceti", TROUGH_FILES)
    sha, index, _mods = D.load_index(inputs, pins, *INDEX)
    deps, selection = _json(files, "deps.json"), _json(files, "selection.json")
    roadmaps, source = _json(files, "roadmaps.json"), _json(files, "source.json")
    records = deps.get("declarations") or {}
    chosen = selection.get("chosen") or {}
    rejected: list[dict] = []

    names, modules = D.name_rows(index, LIBRARY, rejected)

    # The mapped set: deps ∪ chosen ∪ the names the roadmaps' status files name.
    mapped_by: dict[str, set[str]] = {}
    for n in records:
        mapped_by.setdefault(n, set()).add("deps")
    for n in chosen:
        mapped_by.setdefault(n, set()).add("selection")
    on_roadmaps: dict[str, set[str]] = {}
    for rm in roadmaps.get("roadmaps") or []:
        path = rm.get("path") if isinstance(rm, dict) else None
        for n in (rm.get("names") or []) if isinstance(path, str) else []:
            if isinstance(n, str):
                mapped_by.setdefault(n, set()).add("roadmaps")
                on_roadmaps.setdefault(n, set()).add(f"tauceti:{path}")
    for n in deps.get("missing") or []:
        rejected.append({"kind": "decl", "reason": "trough_missing", "candidate": n})

    docs = D.docs_commit(pins, INDEX[0])
    why = stated_reason(source, deps, docs)
    rev = source.get("rev") if isinstance(source.get("rev"), str) else None
    rows, ids = [], {}
    for n in sorted(mapped_by):
        if n not in modules:
            rejected.append({"kind": "decl", "reason": "not_in_index", "candidate": n,
                             "mapped_by": sorted(mapped_by[n])})
            continue
        info = records.get(n)
        row = {"id": D.decl_id(LIBRARY, n), "name": n, "module": modules[n], "kind": index[n].get("kind"),
               **D.trough_fields(n, info, modules[n], rejected, repos=REPOS, commit=rev),
               "mapped_by": sorted(mapped_by[n]),
               "selection": chosen.get(n) if isinstance(chosen.get(n), str) else None,
               "roadmaps": sorted(on_roadmaps.get(n, ()))}
        if why is not None:
            row.update(stated=None, stated_reason=why)
        elif not isinstance(info, dict) or not isinstance(info.get("sorry"), bool):
            row.update(stated=None, stated_reason="not_in_deps")
        else:
            row["stated"] = info["sorry"]
        rows.append(row)
        ids[n] = row["id"]

    evidence = {"repo": D.TROUGH_REPO, "commit": tree.commit, "path": "data/tauceti/deps.json"}
    edges = D.dep_edges(records, ids, evidence, rejected)

    data = {"names.jsonl": D.jsonl_bytes(names), "decls.jsonl": D.jsonl_bytes(rows),
            "deps.jsonl": D.jsonl_bytes(edges), "rejected.jsonl": D.jsonl_bytes(D.rejected_rows(rejected))}
    counts = {
        "names": len(names),
        "decls": len(rows),
        "decls_by_mapped_by": D.count(rows, lambda r: "+".join(r["mapped_by"])),
        "decls_by_kind": D.count(rows, lambda r: str(r["kind"])),
        "decls_by_stated": D.count(rows, lambda r: str(r["stated"]).lower() if r["stated"] is not None
                                   else f"null/{r['stated_reason']}"),
        "deps_by_kind": D.count(edges, lambda r: r["kind"]),
        "rejected_by_reason": D.count(rejected, lambda r: f"{r['kind']}/{r['reason']}"),
    }
    manifest = {
        "schema": SCHEMA,
        "producer": PRODUCER,
        "inputs": {
            "trough": D.trough_input(tree, pins, "tauceti", files,
                                     tauceti_repo=source.get("repo"), tauceti_rev=source.get("rev"),
                                     deps_tauceti_rev=deps.get("tauceti_rev"),
                                     selection_tauceti_rev=selection.get("tauceti_rev"),
                                     roadmaps_repo=roadmaps.get("repo"), roadmaps_rev=roadmaps.get("rev")),
            INDEX[0]: D.index_input(pins, *INDEX, sha),
        },
        # `stated` is read from trough only when trough analysed the commit the docs show.
        "stated": {"trough_tauceti_rev": source.get("rev"), "docs_commit": docs, "reason": why},
        "counts": counts,
        "attribution": ATTRIBUTION,
    }
    return D.seal(out, data, manifest)
