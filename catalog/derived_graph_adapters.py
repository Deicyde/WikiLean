"""Pure catalog derivations over explicit, already verified input objects.

These adapters perform no acquisition, cache lookup, checkout search or output
publication. CSV inputs are iterables so the complete formal dependency corpus
can be processed without loading millions of rows into memory.
"""
from __future__ import annotations

import collections
import copy
import math
import re
from typing import Iterable, Mapping

import build_concept_layer
import build_graph_v2
import build_hierarchy
import ingest_theorem_graph
import lift_formal_edges


def concept_layer(tagged: Mapping[str, list[dict]]) -> list[dict]:
    if set(tagged) != set(build_concept_layer.TAGGED):
        raise ValueError("both explicitly selected tagging audit trails are required")
    by_title = {}
    for filename in build_concept_layer.TAGGED:
        for row in tagged[filename]:
            by_title[row["title"]] = row
    by_qid = {}
    for row in by_title.values():
        qid = row.get("wikidata_qid")
        if qid:
            by_qid[qid] = build_concept_layer.merge(by_qid.get(qid), row)
    return [row for _, row in sorted(by_qid.items())]


def prior_nodes(concepts: list[dict]) -> list[dict]:
    """Reproduce merge_graph's node projection without cached graph edges."""
    return [{"qid": row["qid"],
             "label": row.get("primary_title") or (row.get("titles") or [row["qid"]])[0],
             "slug": row.get("article_slug"), "primary_decl": row.get("primary_decl"),
             "module": row.get("module"), "status": row.get("status"),
             "importance": row.get("importance")} for row in concepts]


def source_rescues(declarations: Iterable[str], source_text: Iterable[str]) -> set[str]:
    """Existing short-name source backstop, confined to the pinned Mathlib tree."""
    patterns = {}
    for declaration in declarations:
        segment = declaration.split(".")[-1]
        patterns[declaration] = re.compile(
            r"(theorem|lemma|def|abbrev|structure|class|instance|inductive) +"
            r"([A-Za-z0-9_'.«»]+\.)?" + re.escape(segment) + r"($|[^A-Za-z0-9_'])",
            re.MULTILINE)
    found = set()
    for text in source_text:
        for name, pattern in patterns.items():
            if name not in found and pattern.search(text):
                found.add(name)
    return found


def formal_edges(decl_to_qid: dict[str, list[str]], statements: Iterable[dict],
                 dependencies: Iterable[dict]) -> list[dict]:
    interest = set(decl_to_qid)
    id_to_decl = {row["statement_id"]: row["decl_name"] for row in statements
                  if row.get("decl_name") in interest}
    edges = {}
    for row in dependencies:
        source = id_to_decl.get(row["src_id"])
        target = id_to_decl.get(row["dep_id"])
        if source is None or target is None or source == target:
            continue
        kind = row.get("edge_type") or "dep"
        if kind == "docref":
            continue
        bucket = lift_formal_edges.BUCKET.get(kind)
        pair = (source, target)
        for left in decl_to_qid[source]:
            for right in decl_to_qid[target]:
                if left == right:
                    continue
                edge = edges.setdefault((left, right), {"decls": [], "pairs": set(),
                    "w": {"sig": set(), "def": set(), "proof": set()}, "types": set()})
                edge["types"].add(kind)
                if bucket is not None:
                    edge["w"][bucket].add(pair)
                if pair not in edge["pairs"]:
                    edge["pairs"].add(pair)
                    if len(edge["decls"]) < lift_formal_edges.MAX_WITNESS:
                        edge["decls"].append([source, target])
    return [{"from": left, "to": right, "source": "mathlib", "decls": value["decls"],
             "weight": len(value["pairs"]), "w_types": {key: len(rows) for key, rows in value["w"].items()},
             "edge_types": sorted(value["types"])} for (left, right), value in edges.items()]


def concept_graph(*, grounding: list[dict], overrides: list[dict], crossrefs: dict,
                  prior: list[dict], annotations: list[tuple[str, dict]], oracle: dict,
                  mathlib_text: Iterable[str], statements: Iterable[dict],
                  dependencies: Iterable[dict], wikidata_edges: Iterable[dict]) -> dict:
    declarations = oracle.get("declarations")
    if not isinstance(declarations, dict) or not declarations:
        raise ValueError("the explicit declaration oracle must be nonempty")
    concepts = copy.deepcopy(grounding)
    proposed = {form["decl"] for concept in concepts for form in (concept.get("formalizations") or [])}
    valid = set(declarations) | source_rescues(proposed - set(declarations), mathlib_text)
    changes = {}
    for row in overrides:
        changes.setdefault(row["qid"], {}).update(row.get("set") or {})
    previous = {row["qid"]: row for row in prior}
    nodes = []
    mapping = collections.defaultdict(list)
    roles = {}
    for concept in concepts:
        qid = concept["qid"]
        forms = [form for form in (concept.get("formalizations") or []) if form["decl"] in valid]
        override = changes.get(qid) or {}
        build_graph_v2.apply_match_kind_overrides(forms, override)
        old = previous.get(qid) or {}
        status = build_graph_v2.derive_status(forms, concept.get("status"))
        if "status" in override:
            status = override["status"]
        if not forms:
            status = "not_formalized"
        xrefs = crossrefs.get("xrefs", {}).get(qid) or {}
        nodes.append({"qid": qid,
            "label": concept.get("label") or old.get("label") or (concept.get("slug") or "").replace("_", " ") or qid,
            "slug": concept.get("slug") or old.get("slug"),
            "primary_decl": forms[0]["decl"] if forms else None,
            "module": forms[0].get("module") if forms else old.get("module"),
            "status": status, "importance": old.get("importance") or "Mid",
            "formalizations": [{key: form.get(key) for key in ("decl", "module", "library", "match_kind", "confidence")}
                               for form in forms],
            "xrefs": xrefs, "xrefs_keys": sorted(xrefs), "arxiv": concept.get("arxiv") or [],
            "is_new": qid not in previous})
        for form in forms:
            mapping[form["decl"]].append(qid)
            roles.setdefault(form["decl"], {})[qid] = "formalization"
    slug_to_qid = {node["slug"]: node["qid"] for node in nodes if node.get("slug")}
    for path, article in sorted(annotations):
        if path.endswith(".agent1.json"):
            continue
        qid = slug_to_qid.get(article.get("slug"))
        if qid:
            for annotation in article.get("annotations") or []:
                declaration = (annotation.get("mathlib") or {}).get("decl")
                if declaration:
                    mapping[declaration].append(qid)
                    roles.setdefault(declaration, {}).setdefault(qid, "citation")
    mapping = {key: sorted(set(value)) for key, value in mapping.items()}
    qids = {node["qid"] for node in nodes}
    edges = [edge for edge in formal_edges(mapping, statements, dependencies)
             if edge["from"] in qids and edge["to"] in qids]
    seen = set()
    for row in wikidata_edges:
        source, target = row.get("s"), row.get("o")
        if source in qids and target in qids and source != target and (source, target) not in seen:
            seen.add((source, target))
            edges.append({"from": source, "to": target, "source": "wikidata",
                          "props": [{"p": row.get("p"), "label": row.get("p_label", "")}]})
    return {"concept-graph": {"nodes": nodes, "edges": edges},
            "decl-to-qid": mapping, "decl-qid-roles": roles}


def hierarchy(statements: Iterable[dict], metadata: dict) -> dict:
    tries, files, declarations = {}, {}, {}
    count = 0
    for row in statements:
        module = row.get("module") or ""
        if not module:
            continue
        count += 1
        parts = module.split(".")
        library = parts[0]
        declarations[library] = declarations.get(library, 0) + 1
        if row.get("file_path"):
            files.setdefault(library, set()).add(row["file_path"])
        node = tries.setdefault(library, {"n": 0, "direct": 0, "kids": {}})
        for part in parts[1:]:
            node = node["kids"].setdefault(part, {"n": 0, "direct": 0, "kids": {}})
            node["n"] += 1
        node["direct"] += 1
    libraries = {name: {"kind": build_hierarchy.LIBRARY_KIND.get(name, "math"),
        "n_decls": declarations[name], "n_files": len(files.get(name, ())),
        "modules": {key: build_hierarchy.emit(value, 1) for key, value in trie["kids"].items()}}
        for name, trie in tries.items()}
    for (library, components), note in build_hierarchy.SUPERSEDED.items():
        node = libraries.get(library, {}).get("modules", {})
        for component in components[:-1]:
            node = node.get(component, {}).get("sub", {})
        target = node.get(components[-1]) if node else None
        if target is not None:
            target.update({"superseded": True, "superseded_note": note})
    subfields = []
    for library, node in sorted(libraries.items(), key=lambda item: -item[1]["n_decls"]):
        for key, value in node["modules"].items():
            build_hierarchy.collect_subfields(library, value, library + "." + key, False, subfields)
    subfields.sort(key=lambda row: -row["n_decls"])
    depth = max((1 + build_hierarchy.tree_depth(module) for node in libraries.values()
                 for module in node["modules"].values()), default=0)
    return {"meta": {
        "source": "uw-math-ai/math-graph statement_formal.csv (TheoremGraph)",
        "source_revision": metadata["revision"], "source_url": metadata["file_url"],
        "source_bytes": metadata["size"], "source_sha256": metadata["sha256"],
        "n_libraries": len(libraries), "n_decls": count, "n_subfields": len(subfields),
        "max_depth": build_hierarchy.MAX_DEPTH, "max_depth_effective": depth,
        "split_threshold": build_hierarchy.SPLIT_AT,
        "note": "BRAIN levels 1-3: library -> module -> subfield. Wikidata concepts "
                "attach at their altitude; Lean decls are the leaves; arXiv informal "
                "statements attach beside their matched decls. Nodes > split_threshold "
                "split one more path component recursively; decls sitting directly on "
                "a split node are its n_direct (and a direct:true subfield row)."},
        "libraries": dict(sorted(libraries.items(), key=lambda item: -item[1]["n_decls"])), "subfields": subfields}


def theoremgraph_links(*, prior: list[dict], annotations: list[tuple[str, dict]],
                       matches: Iterable[dict], metadata: dict, tier: str = "affirmed") -> dict:
    if tier != "affirmed":
        raise ValueError("this normalization profile requires the explicitly reviewed affirmed tier")
    primary, cited = collections.defaultdict(set), collections.defaultdict(set)
    slugs = {row["slug"]: row["qid"] for row in prior if row.get("slug")}
    for row in prior:
        if row.get("primary_decl") and row.get("qid"):
            primary[row["primary_decl"]].add(row["qid"])
    for path, article in sorted(annotations):
        if path.endswith(".agent1.json"):
            continue
        qid = slugs.get(article.get("slug"))
        if qid:
            for annotation in article.get("annotations") or []:
                name = (annotation.get("mathlib") or {}).get("decl")
                if name:
                    cited[name].add(qid)
    interests = set(primary) | set(cited)
    links, seen = collections.defaultdict(list), collections.defaultdict(set)
    for row in matches:
        if row.get("gpt54_label", "") not in ingest_theorem_graph.AFFIRM or row["formal_decl"] not in interests:
            continue
        name = row["formal_decl"]
        similarity = round(float(row["sim"]), 3) if row.get("sim") else None
        if similarity is not None and not math.isfinite(similarity):
            raise ValueError("matching similarity must be finite")
        link = {"decl": name, "arxiv_id": row["arxiv_id"], "ref": row.get("informal_ref") or "",
                "title": row.get("paper_title") or "", "sim": similarity,
                "gpt54": row.get("gpt54_label"), "deepseek": row.get("deepseek_label")}
        for qid in sorted(primary.get(name, set()) | cited.get(name, set())):
            key = (name, row["arxiv_id"])
            if key not in seen[qid]:
                seen[qid].add(key)
                links[qid].append({**link, "primary": qid in primary.get(name, set())})
    order = {"exact": 0, "inexact": 1}
    for values in links.values():
        values.sort(key=lambda row: (not row["primary"], order.get(row["gpt54"], 9), -(row["sim"] or 0)))
        del values[ingest_theorem_graph.MAX_LINKS_PER_QID:]
    return {"_meta": {
        "source": "uw-math-ai/theorem-matching (TheoremGraph)", "paper": "arXiv:2606.25363",
        "license": "CC-BY-SA-4.0", "attribution": "Matches from TheoremGraph (Math-Graph / theorem-matching, "
        "UW Math-AI), arXiv:2606.25363, CC-BY-SA-4.0. Stored as link "
        "facts only; arXiv papers retain their own licenses.", "tier": tier,
        "affirm_labels": sorted(ingest_theorem_graph.AFFIRM), "source_revision": metadata["revision"],
        "source_url": metadata["file_url"], "source_sha256": metadata["sha256"], "source_bytes": metadata["size"],
        "n_concepts": len(links), "n_links": sum(len(values) for values in links.values())},
        "links": {qid: links[qid] for qid in sorted(links)}}
