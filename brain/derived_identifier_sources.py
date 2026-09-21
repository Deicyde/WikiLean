"""Pure Mathlib tag and MathWorld identifier reductions over reviewed v3 parents.

The input plan's exact parent manifest IDs are its explicit review boundary.
Upstream specialized exporters must be verified before those IDs are approved.
This reducer verifies the complete parent object/evidence closure, then uses
only captured immutable normalized objects. No website, cache, or oracle lookup.
"""
from __future__ import annotations

import collections
import copy
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
for directory in (ROOT / "brain", ROOT / "brain/ingest", ROOT / "catalog"):
    sys.path.append(str(directory))
import wikidata_crossref_sources as io
import harvest_mathlib_tags as tags
import mathworld
import common
import build_context

contracts = io.contracts
PLAN_SCHEMA = "wikilean.derived-identifier-plan/v1"
EXPORT_SCHEMA = "wikilean.derived-identifier-export/v1"
PROFILE_SCHEMA = "wikilean.derived-identifier-profiles/v1"
REGISTRY = ROOT / "brain/derived_identifier_profiles.json"
PHYSICAL_ROOT = "derived_identifiers"
PARENTS = {"mathlib-source", "mathlib-docs", "wikidata-wbgetentities", "wikidata-crossrefs", io.CURATED_SOURCE}
TOOL_FILES = tuple(sorted((set(io.TOOL_FILES) - {"brain/export_wikidata_crossrefs.py"}) |
    {"brain/derived_identifier_sources.py", "brain/export_derived_identifiers.py", "catalog/harvest_mathlib_tags.py",
     "brain/ingest/mathworld.py", "brain/ingest/common.py", "brain/build_context.py"}))


def origins():
    io.origins()
    for module, path in ((tags, "catalog/harvest_mathlib_tags.py"), (mathworld, "brain/ingest/mathworld.py"),
        (common, "brain/ingest/common.py"), (build_context, "brain/build_context.py")):
        io.require(Path(getattr(module, "__file__", "")).resolve() == ROOT / path, "unexpected identifier helper origin: " + path)
    io.require(mathworld.common is common and common.seal_external_pair_meta is build_context.seal_external_pair_meta and
        tags.read_text_snapshot is io.git_snapshot.read_text_snapshot, "identifier helpers use different imported dependencies")


origins()
LOADED = {path: io.sha(io.read(ROOT / path)) for path in TOOL_FILES if path != "brain/export_derived_identifiers.py"}


def profile_id(profile):
    return contracts.domain_hash("wikilean.derived-identifier-profile.v1", {"files": profile["files"]})


def profiles():
    raw = io.read(REGISTRY)
    value = io.exact(io.parse(raw, "profiles"), {"schema", "current_profile", "profiles"}, "profiles")
    io.require(value["schema"] == PROFILE_SCHEMA and raw == io.canonical(value) and isinstance(value["profiles"], list), "invalid profile registry")
    ids = []
    for profile in value["profiles"]:
        io.exact(profile, {"profile_id", "files"}, "profile")
        io.require(isinstance(profile["files"], list), "invalid helper closure")
        paths = []
        for item in profile["files"]:
            io.exact(item, {"path", "sha256"}, "helper")
            contracts.validate_literal_relative_path(item["path"], "helper path")
            contracts._digest(item["sha256"], "helper hash")
            paths.append(item["path"])
        io.require(paths == sorted(set(paths)) and "brain/derived_identifier_sources.py" in paths and
            profile_id(profile) == profile["profile_id"], "invalid whole helper generation")
        ids.append(profile["profile_id"])
    io.require(ids == sorted(set(ids)) and value["current_profile"] in ids, "invalid current profile")
    return value


def current_profile():
    origins()
    value = profiles()
    profile = next(p for p in value["profiles"] if p["profile_id"] == value["current_profile"])
    actual = {path: io.sha(io.read(ROOT / path)) for path in TOOL_FILES}
    io.require(profile["files"] == [{"path": path, "sha256": actual[path]} for path in TOOL_FILES], "unreviewed current identifier implementation")
    io.require(all(actual[path] == digest for path, digest in LOADED.items()), "loaded identifier helper changed")
    return profile


def validate_plan(plan):
    io.exact(plan, {"schema", "parents", "reviewed_parent_manifest_ids"}, "identifier plan")
    io.require(plan["schema"] == PLAN_SCHEMA and isinstance(plan["parents"], list) and
        [source["source"] for source in plan["parents"]] == sorted(PARENTS), "exact sorted identifier parent closure required")
    io.require(isinstance(plan["reviewed_parent_manifest_ids"], dict) and set(plan["reviewed_parent_manifest_ids"]) == PARENTS,
        "every identifier parent requires an explicitly reviewed manifest identity")
    for value in plan["reviewed_parent_manifest_ids"].values():
        contracts._hash(value, "reviewed parent")
    return plan


def physical(ref, roots):
    io.require(ref["root"] in roots, "missing parent physical root: " + ref["root"])
    contracts.validate_literal_relative_path(ref["path"], "parent member")
    path = roots[ref["root"]] / ref["path"]
    io.real_path(path)
    return path


def checked(raw, ref):
    io.require(len(raw) == ref["bytes"] and io.sha(raw) == ref["sha256"], "parent bytes differ from reviewed source object")
    return raw


def capture_parents(plan, roots):
    validate_plan(plan)
    sources = {s["source"]: copy.deepcopy(s) for s in plan["parents"]}
    manifests = {name: io.source_plan_contracts._source_manifest_from_plan(source, "identifier parent") for name, source in sources.items()}
    io.require({name: m["source_manifest_id"] for name, m in manifests.items()} == plan["reviewed_parent_manifest_ids"],
        "parent differs from explicitly reviewed source manifest identity")
    by_id = {m["source_manifest_id"]: m for m in manifests.values()}
    objects = {(name, item["name"]): item for name, source in sources.items() for item in source["objects"]}
    selected = {(name, obj) for name, obj in objects if
        (name == "mathlib-source" and (obj == "git_tree" or obj.startswith("file-"))) or
        (name == "mathlib-docs" and obj in {"declaration_oracle", "provenance"}) or
        (name == "wikidata-crossrefs" and obj in {"wikidata_crossrefs", "requested_qid_scope"}) or
        (name == io.CURATED_SOURCE and obj == "source_registry")}
    captured = {}
    for name, source in sources.items():
        curated = None
        if source["source_kind"] == "curated_git_tree":
            io.require(name == io.CURATED_SOURCE and len(source["objects"]) == 1 and
                source["objects"][0]["path"] == io.REGISTRY_PATH, "unexpected curated identifier parent selection")
            curated, tree, _proof, _tool = io.capture_git(roots[source["objects"][0]["root"]], source["pin"]["value"])
            io.require(tree == source["pin"]["tree"], "curated parent tree differs")
        for item in source["objects"]:
            raw = checked(curated if curated is not None else io.read(physical(item, roots)), item)
            key = (name, item["name"])
            if key in selected:
                io.require("normalized" in item["roles"], "identifier input must be normalized")
                captured[key] = raw
    lineages = {}
    for name, source in sources.items():
        if source["source_kind"] == "curated_git_tree":
            continue
        evidence, receipts, preimages = source["evidence"], {}, {}
        for ref in evidence["acquisition_receipts"]:
            receipts[ref["acquisition_receipt_id"]] = io.parse(checked(io.read(physical(ref, roots)), ref), "parent receipt")
        ref = evidence["normalization_lineage"]
        lineage = io.parse(checked(io.read(physical(ref, roots)), ref), "parent lineage")
        lineages[name] = lineage
        for ref in evidence["request_parameter_preimages"]:
            checked(io.read(physical(ref, roots)), ref)
            preimages[ref["parameters_sha256"]] = {key: ref[key] for key in ("parameters_sha256", "bytes", "media_type")}
        io.require(set(lineage["parent_source_manifest_ids"]) <= set(by_id), "missing identifier parent evidence ancestor")
        contracts.validate_source_manifest_evidence_documents(manifests[name], receipts=receipts, lineage=lineage,
            request_parameter_preimages=preimages, parent_source_manifests={key: by_id[key] for key in lineage["parent_source_manifest_ids"]})
    return sources, manifests, objects, captured, lineages


def reduce_mathlib(manifests, objects, captured, lineages):
    source, docs = manifests["mathlib-source"], manifests["mathlib-docs"]
    io.require(source["source_kind"] == "acquired_dataset" and source["pin"]["type"] == "git_commit", "Mathlib requires acquired immutable Git content")
    io.require(source["source_manifest_id"] in lineages["mathlib-docs"]["parent_source_manifest_ids"], "oracle has no selected Mathlib source parent")
    tree = io.parse(captured[("mathlib-source", "git_tree")], "Mathlib tree")
    io.exact(tree, {"commit", "tree", "entries"}, "Mathlib tree")
    provenance = io.parse(captured[("mathlib-docs", "provenance")], "Mathlib oracle provenance")
    oracle_raw = captured[("mathlib-docs", "declaration_oracle")]
    oracle = io.parse(oracle_raw, "declaration oracle", data=True).get("declarations")
    io.require(isinstance(oracle, dict) and bool(oracle) and all(isinstance(value, dict) for value in oracle.values()), "oracle requires declaration records")
    io.require(tree["commit"] == source["pin"]["value"] == provenance["mathlib_commit"] and tree["tree"] == provenance["mathlib_tree"]
        and io.sha(oracle_raw) == provenance["oracle_sha256"], "Mathlib source and official oracle revision/content differ")
    io.require(source["license"]["expression"] == "Apache-2.0", "Mathlib source does not carry reviewed Apache license")
    entries, used = {}, set()
    for row in tree["entries"]:
        io.exact(row, {"path", "mode", "git_blob", "sha256", "bytes"}, "Mathlib tree member")
        contracts.validate_literal_relative_path(row["path"], "Mathlib member path")
        io.require(row["path"] not in entries, "duplicate Mathlib tree path")
        entries[row["path"]] = row
        key = ("mathlib-source", "file-" + io.sha(row["path"].encode()))
        io.require(key in captured, "Mathlib normalized tree is incomplete")
        checked(captured[key], row)
        io.require(io.git_oid("blob", captured[key]) == row["git_blob"], "Mathlib normalized member differs from its Git blob")
        used.add(key)
    io.require(used == {key for key in captured if key[0] == "mathlib-source" and key[1].startswith("file-")}, "undeclared Mathlib normalized members")
    selected = [(path, row) for path, row in sorted(entries.items()) if path.startswith("Mathlib/") and path.endswith(".lean")]
    io.require(bool(selected), "Mathlib source tree has no Lean files")
    rows, problems = [], []
    oracle_names = set(oracle)
    for path, row in selected:
        io.require(row["mode"] in {"100644", "100755"}, "Mathlib Lean input is not a regular file")
        tags.harvest_text(captured[("mathlib-source", "file-" + io.sha(path.encode()))].decode("utf-8"), path, oracle_names, rows, problems)
    rows.sort(key=lambda row: (row["file"], row["line"], row["db"], row["tag"], row["decl"]))
    n_unverified = sum(bool(row.get("unverified")) for row in rows)
    meta = {"source": "mathlib4 @[stacks]/@[kerodon]/@[wikidata] attributes", "license": "Apache-2.0",
        "commit": tree["commit"], "tree": tree["tree"], "source_manifest_id": source["source_manifest_id"],
        "oracle": {"source_manifest_id": docs["source_manifest_id"], "sha256": io.sha(oracle_raw), "bytes": len(oracle_raw),
            "mathlib_commit": tree["commit"], "revision_status": "verified-official-docs-build-lineage"},
        "n_files": len(selected), "counts": dict(sorted(collections.Counter(row["db"] for row in rows).items())),
        "unverified_rows": n_unverified, "unresolved_dropped": len(problems) - n_unverified,
        "note": "Syntactic committed attributes; generated counterparts are not harvested. Unverified rows retain the plain namespace join."}
    output = {"mathlib_tag_xrefs": b"".join(io.artifact(row) + b"\n" for row in [{"_meta": meta}, *rows]),
              "tag_harvest_diagnostics": io.artifact({"problems": problems})}
    inputs = {("mathlib-source", "git_tree"), ("mathlib-docs", "declaration_oracle"), ("mathlib-docs", "provenance"),
              *(("mathlib-source", "file-" + io.sha(path.encode())) for path, _row in selected)}
    return output, inputs


def reduce_mathworld(manifests, objects, captured):
    raw = captured[("wikidata-crossrefs", "wikidata_crossrefs")]
    crossrefs = io.parse(raw, "Wikidata crossrefs", data=True)
    scope = io.parse(captured[("wikidata-crossrefs", "requested_qid_scope")], "crossref scope")
    registry = captured[(io.CURATED_SOURCE, "source_registry")]
    io.require("mathworld" in io.properties(registry).get("P2812", []) and
        "mathworld" in crossrefs["properties"].get("P2812", []), "MathWorld identifiers require the curated P2812 mapping")
    io.exact(scope, {"schema", "qids"}, "crossref scope")
    io.require(scope["schema"] == io.SCOPE_SCHEMA and isinstance(scope["qids"], list) and
        scope["qids"] == sorted(set(scope["qids"])) and set(crossrefs["xrefs"]) <= set(scope["qids"]), "invalid requested crossref scope")
    qids = {}
    for qid in sorted(crossrefs["xrefs"], key=lambda q: (len(q), q)):
        io.require(bool(io.entities.QID_RE.fullmatch(qid)), "invalid crossref QID")
        values = crossrefs["xrefs"][qid].get("mathworld", [])
        io.require(isinstance(values, list) and all(isinstance(value, str) and value for value in values), "MathWorld identifiers must be concrete strings")
        for slug in values:
            qids.setdefault(slug, qid)  # Same lowest-QID collision rule as the legacy adapter.
    pages, seen, changed, dropped = [], set(), set(), 0
    for slug, qid in sorted(qids.items()):
        pid = common.strip_controls(slug)
        if not pid.strip() or (pid in seen and (pid != slug or pid in changed)):
            dropped += 1
            continue
        io.require(pid not in seen, "duplicate normalized MathWorld identifier")
        if pid != slug:
            changed.add(pid)
        seen.add(pid)
        pages.append({"db": "mathworld", "id": pid, "title": mathworld.slug_title(slug), "url": mathworld.PAGE_URL.format(slug), "qid": qid})
    io.require(bool(pages), "reviewed scope contains no MathWorld identifiers")
    meta = {"db": "mathworld", "n_pages": len(pages), "n_links": 0, "n_links_resolved": 0,
        "n_pages_dropped_bad_id": dropped, "n_links_dropped_bad_id": 0,
        "source_pin": "sha256:" + io.sha(raw), "sitemap_inventory": None}
    meta = build_context.seal_external_pair_meta(meta, pages, [])
    build_context.validate_external_pair("mathworld", meta, pages, meta, [])
    output = {"mathworld_pages": b"".join(io.artifact(row) + b"\n" for row in [{"_meta": meta}, *pages]),
        "mathworld_links": io.artifact({"_meta": meta}) + b"\n",
        "mathworld_derivation": io.canonical({"schema": "wikilean.mathworld-ids-only/v1", "property": "P2812",
            "sitemap": {"state": "absent", "reason": "not acquired; IDs-only derivation from reviewed Wikidata crossrefs"},
            "crossref_source_manifest_id": manifests["wikidata-crossrefs"]["source_manifest_id"],
            "registry_source_manifest_id": manifests[io.CURATED_SOURCE]["source_manifest_id"], "snippets": "absent", "links": "absent"})}
    return output, {("wikidata-crossrefs", "wikidata_crossrefs"), ("wikidata-crossrefs", "requested_qid_scope"),
                    (io.CURATED_SOURCE, "source_registry")}


def build_documents(plan, sources, manifests, objects, captured, lineages, profile, programs, when):
    io.require(profile in profiles()["profiles"] and profile["files"] ==
        [{"path": path, "sha256": io.sha(raw)} for path, raw in sorted(programs.items())], "unreviewed identifier program preimages")
    reduced = {"mathlib-tag-xrefs": reduce_mathlib(manifests, objects, captured, lineages),
               "mathworld-identifiers": reduce_mathworld(manifests, objects, captured)}
    files = {"plan.json": io.canonical(plan), "normalization/profile.json": io.canonical(profile),
        "normalization/configuration.json": io.canonical({"schema": PLAN_SCHEMA,
            "mathlib": "complete-sealed-source-harvest_text; official-same-revision-oracle",
            "mathworld": "reviewed-P2812-only; lowest-QID-collision; no-web-acquisition", "metadata": "clock-free-source-identity"})}
    files.update({"implementation/" + path: raw for path, raw in programs.items()})
    def planned(name, path, roles, media="application/json"):
        raw = files[path]
        item = {"name": name, "root": PHYSICAL_ROOT, "path": path, "sha256": io.sha(raw), "bytes": len(raw),
                "media_type": media, "roles": sorted(roles), "redistribution": "restricted"}
        files.setdefault("objects/sha256/" + item["sha256"], raw)
        return item
    tool = {"name": "wikilean-derived-identifiers", "version": "1", "sha256": io.sha(io.canonical(profile))}
    support = [planned("normalizer_profile", "normalization/profile.json", ["receipt"]),
        planned("normalizer_configuration", "normalization/configuration.json", ["receipt"])]
    support += [planned("normalizer_program_" + str(i), "implementation/" + path, ["receipt"], "text/x-python")
                for i, path in enumerate(sorted(programs))]
    children, child_manifests = [], []
    for source, (output, selected) in reduced.items():
        raw_objects, inputs, normalized = [], [], []
        for key in sorted(selected):
            original = objects[key]
            path = "inputs/sha256/" + original["sha256"]
            files[path] = captured[key]
            item = planned(key[1], path, ["raw"], original["media_type"])
            raw_objects.append(item)
            inputs.append({**io.object_ref(item), "origin": {"kind": "source_manifest", "id": manifests[key[0]]["source_manifest_id"]}})
        for name, raw in sorted(output.items()):
            path = "normalized/" + name + (".jsonl" if name in {"mathlib_tag_xrefs", "mathworld_pages", "mathworld_links"} else ".json")
            files[path] = raw
            normalized.append(planned(name, path, ["normalized"], "application/x-ndjson" if path.endswith(".jsonl") else "application/json"))
        parents = {manifests[key[0]]["source_manifest_id"]: manifests[key[0]] for key in selected}
        schema = "wikilean." + source + "/v1"
        lineage = {"schema": contracts.NORMALIZATION_LINEAGE_SCHEMA_V1, "normalization_lineage_id": "sha256:" + "0" * 64,
            "source": source, "mode": "transform", "acquisition_receipt_ids": [], "parent_source_manifest_ids": sorted(parents),
            "normalization_schema": schema, "configuration_sha256": io.sha(files["normalization/configuration.json"]), "tool": tool,
            "inputs": sorted(inputs, key=lambda item: (item["origin"]["kind"], item["origin"]["id"], item["object"])),
            "outputs": [io.object_ref(item) for item in sorted(normalized, key=lambda item: item["name"])],
            "result": "complete", "audit": {"normalized_at": when}}
        lineage["normalization_lineage_id"] = contracts.normalization_lineage_identity(lineage)
        path = "evidence/" + source + ".json"
        files[path] = io.canonical(lineage)
        child = {"source": source, "source_kind": "sealed_snapshot", "pin": {"type": "dataset_revision", "value": lineage["normalization_lineage_id"]},
            "objects": sorted([*raw_objects, *normalized, *support], key=lambda item: item["name"]),
            "license": {"expression": "Apache-2.0" if source == "mathlib-tag-xrefs" else "LicenseRef-Wikidata-Private-Audit",
                "redistribution": "restricted", "notice": "Derived from explicitly reviewed private source evidence; publication is not approved."},
            "acquisition": tool, "normalization": {"schema": schema, "tool": tool,
                "inputs": sorted(item["name"] for item in raw_objects), "outputs": sorted(output)},
            "evidence": {"acquisition_receipts": [], "request_parameter_preimages": [], "normalization_lineage": {
                "root": PHYSICAL_ROOT, "path": path, "sha256": io.sha(files[path]), "bytes": len(files[path]),
                "media_type": "application/json", "normalization_lineage_id": lineage["normalization_lineage_id"]}}}
        manifest = io.source_plan_contracts._source_manifest_from_plan(child, "derived identifier source")
        contracts.validate_source_manifest_evidence_documents(manifest, receipts={}, lineage=lineage,
            request_parameter_preimages={}, parent_source_manifests=parents)
        children.append(child)
        child_manifests.append(manifest)
        files["source-manifests/" + source + ".json"] = io.canonical(manifest)
    fragment = {"schema": "wikilean.derived-identifier-fragment/v1", "scope": "source-plan-fragment", "physical_root": PHYSICAL_ROOT,
        "source_publishable": False, "redistribution": "restricted", "sources": sorted([*sources.values(), *children], key=lambda source: source["source"]),
        "input_bindings": [{"input_id": input_id, "state": "present", "sources": [source], "members": [{"path": path, "source": source, "object": name}]}
            for input_id, source, name, path in (("external-links", "mathworld-identifiers", "mathworld_links", "mathworld_links.jsonl"),
                ("external-pages", "mathworld-identifiers", "mathworld_pages", "mathworld_pages.jsonl"),
                ("mathlib-tag-xrefs", "mathlib-tag-xrefs", "mathlib_tag_xrefs", "catalog/data/mathlib_tag_xrefs.jsonl"))]}
    files["source-fragment.json"] = io.canonical(fragment)
    document = {"schema": EXPORT_SCHEMA, "normalization_profile_id": profile["profile_id"], "normalized_at": when,
        "source_manifest_ids": sorted(m["source_manifest_id"] for m in child_manifests),
        "files": {path: {"sha256": io.sha(raw), "bytes": len(raw)} for path, raw in sorted(files.items())}}
    document["export_id"] = contracts.domain_hash(EXPORT_SCHEMA, document)
    files["export.json"] = io.canonical(document)
    return files
