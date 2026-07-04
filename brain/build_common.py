#!/usr/bin/env python3
"""Shared, deterministic input loading + graph assembly for the BRAIN builders.

build_nodes.py and build_edges.py both call build() and each writes its own
artifact — the node set and the edge set are one joint computation (decl nodes
exist only for decls referenced by >=1 ontology edge), so the assembly lives
here rather than being duplicated or ordered across the two scripts.

Everything is derived from pinned catalog inputs; there is no LLM on this path.
Node/edge shapes are the brain/SCHEMA.md contract. provenance.source values are
keys of catalog/data/source_registry.json (SCHEMA "Provenance & licensing");
the concrete input artifact is named in provenance.method.
"""
from __future__ import annotations

import csv
import json
import os
import re
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
DATA = ROOT / "catalog" / "data"
CACHE = ROOT / "catalog" / ".cache"
BRAIN_DATA = HERE / "data"

csv.field_size_limit(10 ** 9)

INPUTS = {
    "concept_graph_v2.json": DATA / "concept_graph_v2.json",
    "rebuild_grounding.json": DATA / "rebuild_grounding.json",
    "hierarchy.json": DATA / "hierarchy.json",
    "wikidata_universe.jsonl": DATA / "wikidata_universe.jsonl",
    "universe_extension.jsonl": DATA / "universe_extension.jsonl",
    "wikidata_crossrefs.json": DATA / "wikidata_crossrefs.json",
    "theoremgraph_links.json": DATA / "theoremgraph_links.json",
    "decl_qid_roles_v2.json": DATA / "decl_qid_roles_v2.json",
    "decl_to_qid_v2.json": DATA / "decl_to_qid_v2.json",
    "wikidata_edges.jsonl": ROOT / "catalog" / "mathlib_deps" / "wikidata_edges.jsonl",
    "theorem_matching.csv": CACHE / "theorem_matching.csv",
    "statement_formal.csv": CACHE / "statement_formal.csv",
}
OPTIONAL_INPUTS = {
    "container_links.jsonl": BRAIN_DATA / "container_links.jsonl",
    "discovery_proposals.jsonl": BRAIN_DATA / "discovery_proposals.jsonl",
    "mathlib_tag_xrefs.jsonl": DATA / "mathlib_tag_xrefs.jsonl",
}

KIND_ORDER = ["contains", "formalizes", "mentions", "depends", "relates",
              "xref", "cites", "matches"]

# The xref keys of SCHEMA's edge table (P14534/mathlib is `formalizes` territory,
# kgmid is a KG hub id, not an external DB page — neither becomes an xref edge).
XREF_KEYS = {
    "lmfdb_knowl": "P12987", "nlab": "P4215", "mathworld": "P2812",
    "proofwiki": "P6781", "eom": "P7554", "planetmath": "P7726",
    "oeis": "P829", "metamath": "P12888", "dlmf": "P11497", "msc": "P3285",
}

AFFIRM = {"exact", "inexact"}  # theoremgraph_links _meta.affirm_labels


def _pin(name: str) -> str:
    """ISO date (UTC) of the input file's mtime — the per-edge version pin."""
    return datetime.fromtimestamp(INPUTS.get(name, OPTIONAL_INPUTS.get(name)).stat().st_mtime,
                                  tz=timezone.utc).date().isoformat()


def _majority(counter: Counter) -> str | None:
    if not counter:
        return None
    return sorted(counter.items(), key=lambda kv: (-kv[1], kv[0]))[0][0]


def _lit_id(arxiv_id: str, ref: str) -> str:
    return f"lit:{arxiv_id}#{ref}" if ref else f"lit:{arxiv_id}"


def _edge(src: str, dst: str, kind: str, source: str, method: str, pin: str,
          confidence: str, evidence: dict) -> dict:
    return {"src": src, "dst": dst, "kind": kind,
            "provenance": {"source": source, "method": method, "pin": pin},
            "confidence": confidence, "evidence": evidence}


def _prune(d: dict) -> dict:
    return {k: v for k, v in d.items() if v is not None}


def build() -> tuple[list[dict], list[dict], dict]:
    """Returns (nodes, edges, meta) — both lists fully sorted, byte-deterministic."""
    graph = json.loads(INPUTS["concept_graph_v2.json"].read_text())
    grounding = json.loads(INPUTS["rebuild_grounding.json"].read_text())
    hierarchy = json.loads(INPUTS["hierarchy.json"].read_text())
    roles = json.loads(INPUTS["decl_qid_roles_v2.json"].read_text())
    links_doc = json.loads(INPUTS["theoremgraph_links.json"].read_text())
    links, links_meta = links_doc["links"], links_doc["_meta"]

    qids = {n["qid"] for n in graph["nodes"]}

    # ---- decl universe + module/library resolution -------------------------
    # id = decl:<Library>:<FQ name>; the library must be fixed before ANY edge
    # is emitted, so resolution runs over every source first.
    fdecl_qids: dict[str, list[str]] = defaultdict(list)   # formalization role
    mod_votes: dict[str, Counter] = defaultdict(Counter)
    lib_votes: dict[str, Counter] = defaultdict(Counter)
    for n in graph["nodes"]:
        for f in n.get("formalizations") or []:
            fdecl_qids[f["decl"]].append(n["qid"])
            if f.get("module"):
                mod_votes[f["decl"]][f["module"]] += 1
            if f.get("library"):
                lib_votes[f["decl"]][f["library"]] += 1
    fdecls = set(fdecl_qids)
    mention_pairs = sorted((q, d) for d, m in roles.items()
                           for q, r in m.items() if r == "citation")
    ldecls = {l["decl"] for ls in links.values() for l in ls}
    # @[stacks]/@[kerodon]/@[wikidata] attributes harvested from the mathlib4
    # checkout — loaded before the decl universe is fixed so every gold
    # @[wikidata]-tagged decl becomes a brain node even when no agent pipeline
    # found it independently (27/121 were otherwise absent, per the harvest
    # verifier). Fail-soft: without the harvest the build just loses this layer.
    tag_rows: list[dict] = []
    p = OPTIONAL_INPUTS["mathlib_tag_xrefs.jsonl"]
    if p.exists():
        with p.open() as fh:
            tag_rows = [r for line in fh if line.strip()
                        for r in [json.loads(line)] if "decl" in r]
    else:
        print("NOTE: catalog/data/mathlib_tag_xrefs.jsonl missing — "
              "@[stacks]/@[kerodon] xref edges + @[wikidata] source-tag "
              "provenance skipped", file=sys.stderr)
    source_tagged = {(r["tag"], r["decl"]) for r in tag_rows
                     if r["db"] == "wikidata"}
    decl_set = set(roles) | fdecls | ldecls | {d for _, d in source_tagged}
    # Annotation citations occasionally carry junk like
    # "MonoidAlgebra.instIsSemisimpleModule (Maschke)" — whitespace is never
    # legal in a Lean identifier, so such names can't resolve anywhere. Drop
    # them (and their mention pairs) rather than mint unreachable decl nodes.
    bad_names = {d for d in decl_set if any(c.isspace() for c in d)}
    if bad_names:
        print(f"WARNING: dropping {len(bad_names)} whitespace-bearing decl "
              f"name(s) from annotation citations: {sorted(bad_names)[:3]}",
              file=sys.stderr)
        decl_set -= bad_names
        mention_pairs = [(q, d) for q, d in mention_pairs if d not in bad_names]

    # grounding evidence text, joined by (qid, decl) — the immutable audit trail
    # (match_kind/status overrides are already applied inside concept_graph_v2).
    grounding_note = {(r["qid"], f["decl"]): f.get("evidence")
                      for r in grounding for f in r.get("formalizations") or []}

    # ---- one streaming pass over theorem_matching.csv ----------------------
    csv_mod: dict[str, str] = {}
    slogans: dict[str, str] = {}
    lic_open: dict[str, bool] = {}          # per paper (arxiv_id)
    lit_title: dict[str, str] = {}          # per lit id
    lit_sids: dict[str, dict] = {}          # TheoremGraph UUIDs = session keys only
    match_rows: list[dict] = []             # both-judges-affirmed, grounded decls
    with INPUTS["theorem_matching.csv"].open(newline="") as fh:
        for row in csv.DictReader(fh):
            d = row["formal_decl"]
            if d in decl_set:
                if row["formal_module"] and d not in csv_mod:
                    csv_mod[d] = row["formal_module"]
                if row["formal_slogan"] and d not in slogans:
                    slogans[d] = row["formal_slogan"]
            if row["arxiv_id"] and row["arxiv_id"] not in lic_open:
                lic_open[row["arxiv_id"]] = row["license_open"] == "True"
            if (row["gpt54_label"] in AFFIRM and row["deepseek_label"] in AFFIRM
                    and d in fdecls):
                lid = _lit_id(row["arxiv_id"], row["informal_ref"])
                lit_title.setdefault(lid, row["paper_title"])
                lit_sids.setdefault(lid, {"query_sid": row["query_sid"],
                                          "cand_sid": row["cand_sid"]})
                match_rows.append({
                    "decl": d, "lit": lid, "arxiv_id": row["arxiv_id"],
                    "ref": row["informal_ref"], "title": row["paper_title"],
                    "sim": float(row["sim"]), "gpt54": row["gpt54_label"],
                    "deepseek": row["deepseek_label"],
                })

    # statement_formal.csv: module backstop for decls the matching sample never
    # saw, plus kind + docstring (the snapshot's `body` column is empty, so the
    # code itself comes from the live checkout below)
    unresolved = {d for d in decl_set if d not in mod_votes and d not in csv_mod}
    sf_mod: dict[str, str] = {}
    decl_code: dict[str, dict] = {}
    with INPUTS["statement_formal.csv"].open(newline="") as fh:
        for row in csv.DictReader(fh):
            d = row["decl_name"]
            if d in unresolved and row["module"] and d not in sf_mod:
                sf_mod[d] = row["module"]
            if d in decl_set and d not in decl_code:
                rec = _prune({
                    "decl_kind": row.get("kind") or None,
                    "docstring": (row.get("docstring") or "")[:280] or None,
                })
                if rec:
                    decl_code[d] = rec

    # ---- containers from hierarchy.json ------------------------------------
    lib_meta = hierarchy["libraries"]
    containers: dict[str, dict] = {}
    contains_edges: list[dict] = []
    pin_h = _pin("hierarchy.json")
    snapshot_pin = hierarchy["meta"]["source_sha256"]

    def walk(lib: str, kind: str, name: str, node: dict, parent: str, inherited: bool):
        cid = f"{parent}/{name}"
        superseded = inherited or node.get("superseded", False)
        containers[cid] = _prune({
            "id": cid, "type": "container", "label": name, "library": lib,
            "library_kind": kind, "n_decls": node["n_decls"],
            "n_direct": node.get("n_direct"),
            "superseded": True if superseded else None,
            "superseded_note": node.get("superseded_note"),
        })
        contains_edges.append(_edge(parent, cid, "contains", "theoremgraph",
                                    "hierarchy.json file-tree", pin_h, "high",
                                    {"n_decls": node["n_decls"]}))
        for child, sub in node.get("sub", {}).items():
            walk(lib, kind, child, sub, cid, superseded)

    for lib, L in lib_meta.items():
        root = f"path:{lib}"
        containers[root] = {"id": root, "type": "container", "label": lib,
                            "library": lib, "library_kind": L["kind"],
                            "n_decls": L["n_decls"], "n_files": L["n_files"]}
        for name, node in L["modules"].items():
            walk(lib, L["kind"], name, node, root, False)

    # ---- decl nodes + their containment placement --------------------------
    def resolve(d: str) -> tuple[str, str | None]:
        module = _majority(mod_votes[d]) or csv_mod.get(d) or sf_mod.get(d)
        lib = _majority(lib_votes[d])
        if not lib:
            root = module.split(".", 1)[0] if module else None
            lib = root if root in lib_meta else "Mathlib"
        return lib, module

    # ---- Lean source snippets from the live checkout ------------------------
    # The snapshot CSV ships no statement bodies, so the decl panel's code
    # comes from the live mathlib4 checkout (Apache-2.0, attribution in _meta;
    # read-only; fail-soft on drift — a renamed file just means no snippet).
    mathlib_src = Path(os.environ.get(
        "BRAIN_MATHLIB_CHECKOUT", "/Users/jack/Desktop/LEAN/mathlib4/Mathlib")).parent
    kw = r"(?:theorem|lemma|def|abbrev|structure|class|instance|inductive|opaque|axiom)"
    by_file: dict[str, list[str]] = defaultdict(list)
    for d in decl_set:
        lib, module = resolve(d)
        if lib == "Mathlib" and module:
            by_file[module].append(d)
    n_snippets = 0
    if mathlib_src.exists():
        for module, decls in by_file.items():
            fp = mathlib_src / (module.replace(".", "/") + ".lean")
            try:
                lines = fp.read_text().splitlines()
            except OSError:
                continue
            for d in decls:
                seg = re.escape(d.split(".")[-1])
                pat = re.compile(rf"^\s*(?:@\[[^\]]*\]\s*)?(?:private\s+|protected\s+"
                                 rf"|noncomputable\s+|nonrec\s+|scoped\s+)*{kw}\s+"
                                 rf"(?:[A-Za-z0-9_'.«»]+\.)?{seg}($|[^A-Za-z0-9_'])")
                for i, line in enumerate(lines):
                    if not pat.match(line):
                        continue
                    snip: list[str] = []
                    for l in lines[i:i + 12]:
                        s = l.rstrip()
                        if snip and not s:
                            break            # blank line = statement header over
                        snip.append(l)
                        if (s.endswith(":=") or s.endswith(":= by") or s.endswith(" by")
                                or s.endswith("where") or s.endswith(":= fun")):
                            break
                    code = "\n".join(snip)[:700]
                    decl_code.setdefault(d, {})["code"] = code
                    n_snippets += 1
                    break
    else:
        print(f"WARNING: mathlib checkout missing at {mathlib_src} — decl code "
              f"snippets skipped (BRAIN_MATHLIB_CHECKOUT to override)", file=sys.stderr)
    print(f"  decl code snippets from the checkout: {n_snippets}/{len(decl_set)}",
          file=sys.stderr)

    decl_id: dict[str, str] = {}
    decl_nodes: list[dict] = []
    n_unplaced = 0
    for d in sorted(decl_set):
        lib, module = resolve(d)
        did = f"decl:{lib}:{d}"
        decl_id[d] = did
        decl_nodes.append(_prune({
            "id": did, "type": "decl", "label": d, "library": lib,
            "module": module, "slogan": slogans.get(d), "pin": snapshot_pin,
            **decl_code.get(d, {}),
        }))
        # placement: deepest hierarchy container prefixing the decl's module
        # (the tree is depth-capped, so this is the file container when the
        # file node exists and the nearest enclosing dir otherwise)
        parts = module.split(".") if module else [lib]
        cur = f"path:{parts[0]}"
        if cur not in containers:
            n_unplaced += 1
            continue
        for comp in parts[1:]:
            if f"{cur}/{comp}" not in containers:
                break
            cur = f"{cur}/{comp}"
        contains_edges.append(_edge(cur, did, "contains", "theoremgraph",
                                    "module-prefix placement", pin_h, "high",
                                    _prune({"module": module})))

    # ---- mathlib source cross-reference tags --------------------------------
    n_source_tagged = 0
    emitted_formalizes: set[tuple[str, str]] = set()   # (qid, bare decl name)

    # ---- ontology edges -----------------------------------------------------
    edges: list[dict] = list(contains_edges)
    pin_g = _pin("concept_graph_v2.json")

    for n in graph["nodes"]:
        for f in n.get("formalizations") or []:
            gold = (n["qid"], f["decl"]) in source_tagged
            n_source_tagged += gold
            emitted_formalizes.add((n["qid"], f["decl"]))
            edges.append(_edge(n["qid"], decl_id[f["decl"]], "formalizes",
                               "mathlib",
                               "@[wikidata] attribute (mathlib4 source)" if gold
                               else "agent+oracle", pin_g,
                               f.get("confidence") or "medium",
                               _prune({"match_kind": f.get("match_kind"),
                                       "module": f.get("module"),
                                       "source_tagged": True if gold else None,
                                       "grounding_note": grounding_note.get(
                                           (n["qid"], f["decl"])),
                                       "verified_by": "build_graph_v2 oracle+checkout"})))

    pin_r = _pin("decl_qid_roles_v2.json")
    for q, d in mention_pairs:
        edges.append(_edge(q, decl_id[d], "mentions", "annotations",
                           "annotation-citation (decl_qid_roles_v2)", pin_r,
                           "high", {"role": "citation"}))

    for e in graph["edges"]:
        if e.get("source") != "mathlib":
            continue
        w = e.get("weight", 0)
        conf = "high" if w >= 5 else "medium" if w >= 2 else "low"
        edges.append(_edge(e["from"], e["to"], "depends", "mathlib_deps",
                           "lift_formal_edges (formal_dependency.csv)", pin_g, conf,
                           {"weight": w, "w_types": e.get("w_types"),
                            "witnesses": e.get("decls") or []}))

    pin_w = _pin("wikidata_edges.jsonl")
    rel_props: dict[tuple[str, str], list] = defaultdict(list)
    with INPUTS["wikidata_edges.jsonl"].open() as fh:
        for line in fh:
            r = json.loads(line)
            if r["s"] in qids and r["o"] in qids:
                rel_props[(r["s"], r["o"])].append({"p": r["p"], "label": r["p_label"]})
    for (s, o), props in sorted(rel_props.items()):
        props = sorted(props, key=lambda p: int(p["p"][1:]))
        edges.append(_edge(s, o, "relates", "wikidata_props", "wikidata-claims",
                           pin_w, "high", {"properties": props}))

    # One edge per (concept, source, page): the dst is the external PAGE id, so
    # two concepts sharing a MathWorld/nLab/LMFDB page become graph-discoverable
    # (the dst is an external identifier, not a node — see the P5d check below).
    pin_x = _pin("wikidata_crossrefs.json")
    n_xref_skipped_keys = 0
    for n in graph["nodes"]:
        for key, values in sorted((n.get("xrefs") or {}).items()):
            if key not in XREF_KEYS:
                n_xref_skipped_keys += 1
                continue
            for v in sorted(values):
                edges.append(_edge(n["qid"], f"xref:{key}:{v}", "xref", key,
                                   "wikidata-property", pin_x, "high",
                                   {"property": XREF_KEYS[key], "value": v}))

    # decl → Stacks/Kerodon tag xrefs, only for decls that are already brain
    # nodes (rows for untracked decls are counted, never minted into nodes)
    n_tag_xref = n_tag_skipped = 0
    seen_tag: set[tuple[str, str]] = set()
    for r in tag_rows:
        if r["db"] not in ("stacks", "kerodon"):
            continue
        if r["decl"] not in decl_id:
            n_tag_skipped += 1
            continue
        key = (decl_id[r["decl"]], f"xref:{r['db']}:{r['tag']}")
        if key in seen_tag:
            continue
        seen_tag.add(key)
        n_tag_xref += 1
        edges.append(_edge(key[0], key[1], "xref", r["db"],
                           f"@[{r['db']}] attribute (mathlib4 source)",
                           snapshot_pin, "high",
                           {"tag": r["tag"], "value": r["tag"], "file": r["file"]}))
    if tag_rows:
        print(f"  mathlib tag xrefs: {n_tag_xref} stacks/kerodon edges "
              f"({n_tag_skipped} rows skipped — decl has no brain node); "
              f"{n_source_tagged} formalizes edges source-tagged @[wikidata]",
              file=sys.stderr)

    # ---- cites + matches (TheoremGraph links + transitive join) ------------
    pin_l = _pin("theoremgraph_links.json")
    pin_m = _pin("theorem_matching.csv")

    def judge_conf(g: str, d: str) -> str:
        return "high" if g == "exact" and d == "exact" else "medium"

    cites: dict[tuple[str, str], dict] = {}
    for q in sorted(links):
        for l in links[q]:
            lid = _lit_id(l["arxiv_id"], l["ref"])
            lit_title.setdefault(lid, l["title"])
            key = (q, lid)
            if key in cites:
                vd = cites[key]["evidence"]["via_decls"]
                if l["decl"] not in vd and len(vd) < 8:
                    vd.append(l["decl"])
                continue
            cites[key] = _edge(q, lid, "cites", "theoremgraph",
                               "theoremgraph_links", pin_l,
                               judge_conf(l["gpt54"], l["deepseek"]),
                               {"via_decls": [l["decl"]], "gpt54": l["gpt54"],
                                "deepseek": l["deepseek"], "sim": l["sim"],
                                "primary": l["primary"],
                                "license_open": lic_open.get(l["arxiv_id"])})
    n_cites_links = len(cites)
    match_rows.sort(key=lambda r: (r["decl"], r["lit"],
                                   -(r["gpt54"] == "exact" and r["deepseek"] == "exact"),
                                   -r["sim"]))
    matches: dict[tuple[str, str], dict] = {}
    for r in match_rows:
        mkey = (decl_id[r["decl"]], r["lit"])
        if mkey not in matches:
            matches[mkey] = _edge(mkey[0], r["lit"], "matches", "theoremgraph",
                                  "theorem_matching dual-judge", pin_m,
                                  judge_conf(r["gpt54"], r["deepseek"]),
                                  {"gpt54": r["gpt54"], "deepseek": r["deepseek"],
                                   "sim": r["sim"],
                                   "license_open": lic_open.get(r["arxiv_id"])})
        for q in sorted(set(fdecl_qids[r["decl"]])):  # transitive join, concept side
            key = (q, r["lit"])
            if key in cites:
                vd = cites[key]["evidence"]["via_decls"]
                if r["decl"] not in vd and len(vd) < 8:
                    vd.append(r["decl"])
                continue
            cites[key] = _edge(q, r["lit"], "cites", "theoremgraph",
                               "theorem_matching transitive-join", pin_m,
                               judge_conf(r["gpt54"], r["deepseek"]),
                               {"via_decls": [r["decl"]], "gpt54": r["gpt54"],
                                "deepseek": r["deepseek"], "sim": r["sim"],
                                "license_open": lic_open.get(r["arxiv_id"])})
    # links-file matches: every affirmed link row is also a decl→lit match
    for q in sorted(links):
        for l in links[q]:
            mkey = (decl_id[l["decl"]], _lit_id(l["arxiv_id"], l["ref"]))
            if mkey not in matches:
                matches[mkey] = _edge(mkey[0], mkey[1], "matches", "theoremgraph",
                                      "theoremgraph_links", pin_l,
                                      judge_conf(l["gpt54"], l["deepseek"]),
                                      {"gpt54": l["gpt54"], "deepseek": l["deepseek"],
                                       "sim": l["sim"],
                                       "license_open": lic_open.get(l["arxiv_id"])})
    edges.extend(cites[k] for k in sorted(cites))
    edges.extend(matches[k] for k in sorted(matches))

    # ---- fail-soft layers ---------------------------------------------------
    # Container links and discovery rows may introduce BRAND-NEW concepts (QIDs
    # outside the graph — fold_proposals fetched their labels/P31 into
    # universe_extension.jsonl) and brand-new decls (oracle/checkout-verified by
    # the fold). Create their nodes here so these layers genuinely GROW the
    # brain rather than only linking it.
    new_concepts: dict[str, dict] = {}
    new_decls: dict[str, dict] = {}
    universe_rec: dict[str, dict] = {}
    for name in ("wikidata_universe.jsonl", "universe_extension.jsonl"):
        if INPUTS[name].exists():
            with INPUTS[name].open() as fh:
                for line in fh:
                    r = json.loads(line)
                    universe_rec.setdefault(r["qid"], r)

    def ensure_concept(qid: str) -> bool:
        if qid in qids or qid in new_concepts:
            return True
        u = universe_rec.get(qid)
        if not u:
            return False
        new_concepts[qid] = _prune({
            "id": qid, "type": "concept", "label": u.get("label"),
            "slug": u.get("enwiki_slug"),
            "description": u.get("description"),
            "altitude_evidence": {"p31": u.get("classes") or [],
                                  "module_span": [], "match_kinds": []},
            "display": {"status": "partial"},
        })
        return True

    p = OPTIONAL_INPUTS["container_links.jsonl"]
    if p.exists():
        pin_c = _pin("container_links.jsonl")
        for line in p.read_text().splitlines():
            if not line.strip():
                continue
            rec = json.loads(line)
            path = rec["path"].removeprefix("path:").replace(".", "/")
            cid = f"path:{path}"
            if not ensure_concept(rec["qid"]) or cid not in containers:
                print(f"WARNING: container_links row skipped (unknown qid/path): "
                      f"{rec.get('qid')} -> {rec.get('path')}", file=sys.stderr)
                continue
            edges.append(_edge(rec["qid"], cid, "formalizes", "mathlib",
                               "container_links", pin_c,
                               rec.get("confidence") or "medium",
                               {"match_kind": rec.get("match_kind", "field"),
                                "note": rec.get("evidence")}))
    else:
        print("NOTE: brain/data/container_links.jsonl missing — "
              "concept→container formalizes layer skipped", file=sys.stderr)

    p = OPTIONAL_INPUTS["discovery_proposals.jsonl"]
    if p.exists():
        pin_d = _pin("discovery_proposals.jsonl")
        known = set(decl_id.values()) | set(containers) | qids
        for line in p.read_text().splitlines():
            if not line.strip():
                continue
            rec = json.loads(line)
            # only verifier-passed rows fold; rejected/unverified rows stay put
            if rec.get("rejected_reason") or rec.get("verified") is not True:
                continue
            src, dst = rec.get("src"), rec.get("dst")
            if rec.get("kind") not in KIND_ORDER:
                print(f"WARNING: discovery proposal skipped (unknown kind): "
                      f"{src} -{rec.get('kind')}-> {dst}", file=sys.stderr)
                continue
            if src not in known and not ensure_concept(src):
                print(f"WARNING: discovery src QID {src} has no universe "
                      f"record — row skipped", file=sys.stderr)
                continue
            if dst.startswith("decl:"):
                # The fold hardcodes lib=Mathlib; if resolve() already placed
                # this decl under another library (TheoremGraph module vote),
                # remap onto the existing node instead of forking a duplicate.
                bare = dst.split(":", 2)[2]
                if dst not in known and bare in decl_id:
                    dst = decl_id[bare]
            if dst not in known and dst not in new_decls and dst.startswith("decl:"):
                lib, d = dst.split(":", 2)[1:]
                module = rec.get("module")
                new_decls[dst] = _prune({
                    "id": dst, "type": "decl", "label": d, "library": lib,
                    "module": module, "slogan": slogans.get(d),
                    "pin": snapshot_pin,
                    **decl_code.get(d, {}),
                })
                parts = module.split(".") if module else [lib]
                cur = f"path:{parts[0]}"
                if cur in containers:
                    for comp in parts[1:]:
                        if f"{cur}/{comp}" not in containers:
                            break
                        cur = f"{cur}/{comp}"
                    edges.append(_edge(cur, dst, "contains", "theoremgraph",
                                       "module-prefix placement", pin_h, "high",
                                       _prune({"module": module})))
            ev = rec.get("evidence") or {}
            mk = ev.get("match_kind")
            if src in new_concepts and mk:
                ae = new_concepts[src]["altitude_evidence"]
                if mk not in ae["match_kinds"]:
                    ae["match_kinds"].append(mk)
                span = "/".join((rec.get("module") or "").split(".")[:2])
                if span and span not in ae["module_span"]:
                    ae["module_span"].append(span)
            if rec["kind"] == "formalizes" and dst.startswith("decl:"):
                bare = dst.split(":", 2)[2]
                emitted_formalizes.add((src, bare))
                if (src, bare) in source_tagged:   # gold pair found by another path
                    ev = {**ev, "source_tagged": True}
            edges.append(_edge(src, dst, rec["kind"], "mathlib",
                               "discovery_proposals (verified)", pin_d,
                               rec.get("confidence") or "medium", ev))
    else:
        print("NOTE: brain/data/discovery_proposals.jsonl missing — "
              "discovery layer skipped", file=sys.stderr)

    # Gold @[wikidata] pairs no pipeline found independently get minted here —
    # a maintainer-reviewed source tag IS a formalizes edge, the strongest kind
    # we have. Their decls joined decl_set above, so the decl node exists; the
    # QID must at least be known to the universe (else counted, never guessed).
    n_gold_minted = n_gold_unknown_qid = 0
    for qid, d in sorted(source_tagged - emitted_formalizes):
        if d not in decl_id:
            continue   # whitespace-filtered or unresolvable name
        if not ensure_concept(qid):
            n_gold_unknown_qid += 1
            continue
        n_gold_minted += 1
        n_source_tagged += 1
        edges.append(_edge(qid, decl_id[d], "formalizes", "mathlib",
                           "@[wikidata] attribute (mathlib4 source)",
                           snapshot_pin, "high",
                           {"match_kind": "exact", "source_tagged": True}))
    if source_tagged:
        print(f"  gold @[wikidata] pairs minted as new formalizes edges: "
              f"{n_gold_minted} ({n_gold_unknown_qid} skipped — QID not in the "
              f"universe)", file=sys.stderr)

    # ---- concept nodes -------------------------------------------------------
    p31: dict[str, list[str]] = {}
    for name in ("wikidata_universe.jsonl", "universe_extension.jsonl"):
        with INPUTS[name].open() as fh:
            for line in fh:
                r = json.loads(line)
                merged = p31.setdefault(r["qid"], [])
                merged.extend(c for c in r.get("classes") or [] if c not in merged)

    concept_nodes = []
    for n in graph["nodes"]:
        span = sorted({"/".join((f.get("module") or "").split(".")[:2])
                       for f in n.get("formalizations") or [] if f.get("module")})
        concept_nodes.append(_prune({
            "id": n["qid"], "type": "concept", "label": n.get("label"),
            "slug": n.get("slug"),
            # Google KG is a hub id (never an xref edge — SCHEMA) but a useful
            # "Also in" chip; carried on the node payload instead
            "kgmid": ((n.get("xrefs") or {}).get("kgmid") or [None])[0],
            "altitude_evidence": {
                "p31": p31.get(n["qid"], []),
                "module_span": span,
                "match_kinds": sorted({f.get("match_kind")
                                       for f in n.get("formalizations") or []
                                       if f.get("match_kind")}),
                "msc": sorted((n.get("xrefs") or {}).get("msc", [])),
            },
            "display": _prune({"primary_decl": n.get("primary_decl"),
                               "status": n.get("status"),
                               "importance": n.get("importance")}),
        }))

    concept_nodes.extend(new_concepts[q] for q in sorted(new_concepts))
    decl_nodes.extend(new_decls[d] for d in sorted(new_decls))

    lit_nodes = [_prune({
        "id": lid, "type": "literature",
        "label": lit_title.get(lid) or lid,
        "arxiv_id": lid[4:].split("#", 1)[0],
        "ref": lid.split("#", 1)[1] if "#" in lid else "",
        "license_open": lic_open.get(lid[4:].split("#", 1)[0]),
        "session_keys": lit_sids.get(lid),
    }) for lid in sorted(lit_title)]

    nodes = (sorted(concept_nodes, key=lambda n: int(n["id"][1:]))
             + [containers[k] for k in sorted(containers)]
             + decl_nodes + lit_nodes)
    edges.sort(key=lambda e: (KIND_ORDER.index(e["kind"]), e["src"], e["dst"]))

    # every non-xref endpoint must be a real node (xref dst is the external DB)
    ids = {n["id"] for n in nodes}
    dangling = [e for e in edges
                if e["src"] not in ids or (e["kind"] != "xref" and e["dst"] not in ids)]
    if dangling:
        raise SystemExit(f"BUG: {len(dangling)} edges with dangling endpoints, "
                         f"first: {dangling[0]}")

    present = {**INPUTS, **{k: v for k, v in OPTIONAL_INPUTS.items() if v.exists()}}
    newest = max(v.stat().st_mtime for v in present.values())
    meta = {
        "schema": "brain/SCHEMA.md",
        # newest input mtime, NOT build time — rebuilds of the same inputs are stable
        "generated_at": datetime.fromtimestamp(newest, tz=timezone.utc)
                        .isoformat(timespec="seconds"),
        "inputs": {k: {"mtime": datetime.fromtimestamp(v.stat().st_mtime, tz=timezone.utc)
                       .isoformat(timespec="seconds"), "bytes": v.stat().st_size}
                   for k, v in sorted(present.items())},
        "licenses": {
            "brain": "CC0-1.0 (WikiLean's own node/edge data)",
            "theoremgraph": links_meta["attribution"],
            "slogans": "decl `slogan` fields are formal_slogan from TheoremGraph "
                       "theorem_matching.csv — CC-BY-SA-4.0, render with source credit",
            "code": "decl `code` snippets are statement headers read from the live "
                    "mathlib4 checkout — Apache-2.0 (mathlib4 contributors), render "
                    "with source credit; `docstring`/`decl_kind` from TheoremGraph "
                    "statement_formal.csv (CC-BY-4.0)",
            "arxiv": "arXiv statement text is never redistributed — ids/titles/labels only",
            "wikidata": "CC0-1.0",
            "mathlib_tags": "@[stacks]/@[kerodon]/@[wikidata] cross-reference tags "
                            "harvested from the mathlib4 source (Apache-2.0, mathlib4 "
                            "contributors) — human-reviewed gold links",
        },
        "counts": {
            "nodes": dict(sorted(Counter(n["type"] for n in nodes).items())),
            "edges": {k: c for k, c in
                      sorted(Counter(e["kind"] for e in edges).items(),
                             key=lambda kv: KIND_ORDER.index(kv[0]))},
        },
        "notes": {
            "decls_without_module": len([d for d in decl_set
                                         if not resolve(d)[1]]),
            "decls_unplaced": n_unplaced,
            "cites_from_links": n_cites_links,
            "cites_from_transitive_join": len(cites) - n_cites_links,
            "xref_values_skipped_nonschema_keys": n_xref_skipped_keys,
            "mathlib_tag_xref_edges": n_tag_xref,
            "mathlib_tag_rows_skipped_no_decl_node": n_tag_skipped,
            "formalizes_source_tagged": n_source_tagged,
        },
    }
    return nodes, edges, meta


def write_jsonl(out: Path, meta: dict, rows: list[dict]) -> None:
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_suffix(".jsonl.tmp")
    with tmp.open("w") as fh:
        fh.write(json.dumps({"_meta": meta}, ensure_ascii=False,
                            separators=(",", ":")) + "\n")
        for r in rows:
            fh.write(json.dumps(r, ensure_ascii=False, separators=(",", ":")) + "\n")
    tmp.replace(out)
