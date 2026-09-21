"""Pure DLMF HTML projection over reviewed immutable source manifests.

The reviewed manifest IDs are the explicit trust root: upstream specialized
verifiers run before plan review. Every parent object and lineage is checked
here; only captured HTML response bytes, scoped crossrefs and Git registry supply data.
"""
from __future__ import annotations

import ast
import re
import types
import copy
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
for directory in (ROOT / "brain", ROOT / "brain/ingest", ROOT / "catalog"):
    sys.path.append(str(directory))
import wikidata_crossref_sources as io
import common
import build_context
import external_pair_normalization as pairs
import dlmf_source_evidence as observation

contracts = io.contracts
PLAN_SCHEMA = "wikilean.dlmf-normalization-plan/v1"
EXPORT_SCHEMA = "wikilean.dlmf-normalization-export/v1"
PROFILE_SCHEMA = "wikilean.dlmf-normalization-profiles/v1"
REGISTRY = ROOT / "brain/dlmf_normalization_profiles.json"
PHYSICAL_ROOT = "dlmf_export"
PARENTS = {"dlmf-page-walk", "wikidata-wbgetentities", "wikidata-crossrefs", io.CURATED_SOURCE}
CHILDREN = ("external-dlmf",)
TOOL_FILES = tuple(sorted((set(io.TOOL_FILES) - {"brain/export_wikidata_crossrefs.py"}) |
    {*pairs.TOOL_FILES, "brain/dlmf_normalization.py", "brain/export_dlmf_normalization.py", "brain/ingest/dlmf.py", "brain/dlmf_source_evidence.py", "brain/mathlib_source_evidence.py"}))


def origins():
    io.origins()
    observation.origins()
    io.require(Path(observation.__file__).resolve() == ROOT / "brain/dlmf_source_evidence.py" and
        observation.contracts is contracts, "DLMF replay helper origin or contract differs")
    pairs.origins()
    io.require(Path(pairs.__file__).resolve() == ROOT / "brain/external_pair_normalization.py", "pair helper origin differs")
    for module, path in ((common, "brain/ingest/common.py"), (build_context, "brain/build_context.py")):
        io.require(Path(getattr(module, "__file__", "")).resolve() == ROOT / path, "unexpected DLMF helper origin: " + path)
    io.require(common.seal_external_pair_meta is build_context.seal_external_pair_meta,
        "DLMF helpers use different imported dependencies")


origins()
LOADED = {path: io.sha(io.read(ROOT / path)) for path in TOOL_FILES if path != "brain/export_dlmf_normalization.py"}


def profile_id(profile):
    return contracts.domain_hash("wikilean.dlmf-normalization-profile.v1", {"files": profile["files"]})


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
        io.require(paths == sorted(set(paths)) and "brain/dlmf_normalization.py" in paths and
            profile_id(profile) == profile["profile_id"], "invalid whole helper generation")
        ids.append(profile["profile_id"])
    io.require(ids == sorted(set(ids)) and value["current_profile"] in ids, "invalid current profile")
    return value


def current_profile():
    origins()
    value = profiles()
    profile = next(p for p in value["profiles"] if p["profile_id"] == value["current_profile"])
    actual = {path: io.sha(io.read(ROOT / path)) for path in TOOL_FILES}
    io.require(profile["files"] == [{"path": path, "sha256": actual[path]} for path in TOOL_FILES], "unreviewed current DLMF implementation")
    io.require(all(actual[path] == digest for path, digest in LOADED.items()), "loaded DLMF helper changed")
    return profile


def validate_plan(plan):
    io.exact(plan, {"schema", "parents", "reviewed_parent_manifest_ids"}, "DLMF plan")
    io.require(plan["schema"] == PLAN_SCHEMA and isinstance(plan["parents"], list) and
        [source["source"] for source in plan["parents"]] == sorted(PARENTS), "exact sorted DLMF parent closure required")
    io.require(isinstance(plan["reviewed_parent_manifest_ids"], dict) and set(plan["reviewed_parent_manifest_ids"]) == PARENTS,
        "every DLMF parent requires an explicitly reviewed manifest identity")
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
    manifests = {name: io.source_plan_contracts._source_manifest_from_plan(source, "DLMF parent") for name, source in sources.items()}
    io.require({name: m["source_manifest_id"] for name, m in manifests.items()} == plan["reviewed_parent_manifest_ids"],
        "parent differs from explicitly reviewed source manifest identity")
    by_id = {m["source_manifest_id"]: m for m in manifests.values()}
    objects = {(name, item["name"]): item for name, source in sources.items() for item in source["objects"]}
    selected = {(name, obj) for name, obj in objects if
        (name == "dlmf-page-walk" and obj in {"page_transcript", "normalization_plan"}) or
        (name == "wikidata-crossrefs" and obj in {"wikidata_crossrefs", "requested_qid_scope"}) or
        (name == io.CURATED_SOURCE and obj == "source_registry")}
    captured = {}
    for name, source in sources.items():
        curated = None
        if source["source_kind"] == "curated_git_tree":
            io.require(name == io.CURATED_SOURCE and len(source["objects"]) == 1 and
                source["objects"][0]["path"] == io.REGISTRY_PATH, "unexpected curated DLMF parent selection")
            curated, tree, _proof, _tool = io.capture_git(roots[source["objects"][0]["root"]], source["pin"]["value"])
            io.require(tree == source["pin"]["tree"], "curated parent tree differs")
        for item in source["objects"]:
            raw = checked(curated if curated is not None else io.read(physical(item, roots)), item)
            key = (name, item["name"])
            if key in selected:
                io.require("normalized" in item["roles"] or (key == ("dlmf-page-walk", "normalization_plan") and item["roles"] == ["receipt"]), "DLMF input must be normalized or bound observation-plan support")
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
        io.require(set(lineage["parent_source_manifest_ids"]) <= set(by_id), "missing DLMF parent evidence ancestor")
        contracts.validate_source_manifest_evidence_documents(manifests[name], receipts=receipts, lineage=lineage,
            request_parameter_preimages=preimages, parent_source_manifests={key: by_id[key] for key in lineage["parent_source_manifest_ids"]})
    return sources, manifests, objects, captured, lineages


PARSER_SYMBOLS = {"BASE", "section_qids"}
CONFIGURATION = {"schema": "wikilean.dlmf-normalization/v1", "source": "exact ordered index/TOC/section transcript and bound original walk plan",
    "observation": "independent-live-requests/no-snapshot", "replay": "complete index and36TOCs derive every required section; original reviewed byte/request/graph bounds",
    "projection": "legacy numeric section ordering, title/href parser, section URLs and section_qids; no snippets",
    "parser_symbols": sorted(PARSER_SYMBOLS), "crossref_join": "reviewed P11497 requested-QID scope; lowest QID per external value then existing lexical equation/subsection-to-section collapse",
    "pair_normalization": "exact common.emit pure prefix through validation; content-addressed source pin",
    "publication": "restricted private evidence; no new permission"}


def dlmf_qids(captured):
    refs = io.parse(captured[("wikidata-crossrefs", "wikidata_crossrefs")], "crossrefs", data=True)
    scope = io.parse(captured[("wikidata-crossrefs", "requested_qid_scope")], "scope")
    io.exact(scope, {"schema", "qids"}, "scope")
    io.require(scope["schema"] == io.SCOPE_SCHEMA and isinstance(scope["qids"], list) and
        scope["qids"] == sorted(set(scope["qids"])) and set(refs["xrefs"]) <= set(scope["qids"]), "invalid requested crossref scope")
    io.require(all(isinstance(qid, str) and io.entities.QID_RE.fullmatch(qid) for qid in scope["qids"]), "invalid scope QID")
    io.require("dlmf" in io.properties(captured[(io.CURATED_SOURCE, "source_registry")]).get("P11497", []) and
        "dlmf" in refs["properties"].get("P11497", []), "DLMF identifiers require the curated P11497 mapping")
    qmap = {}
    for qid in sorted(refs["xrefs"], key=lambda q: (len(q), q)):
        values = refs["xrefs"][qid].get("dlmf", [])
        io.require(isinstance(values, list) and all(isinstance(value, str) and value for value in values), "DLMF IDs must be concrete strings")
        for value in values:
            qmap.setdefault(value, qid)
    return qmap


def selected_parser(program, qmap):
    tree = ast.parse(program, filename="sealed:brain/ingest/dlmf.py")
    selected, names = [], []
    for node in tree.body:
        name = node.name if isinstance(node, ast.FunctionDef) else (
            node.targets[0].id if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name) else None)
        if name in PARSER_SYMBOLS:
            selected.append(node); names.append(name)
    io.require(set(names) == PARSER_SYMBOLS and len(names) == len(set(names)), "legacy DLMF parser selector differs")
    module = ast.Module(body=[ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0), *selected], type_ignores=[])
    def qids(db):
        io.require(db == "dlmf", "unexpected crossref family")
        return dict(qmap)
    namespace = {"re": re, "common": types.SimpleNamespace(qid_map=qids)}
    exec(compile(ast.fix_missing_locations(module), "sealed:dlmf-projection", "exec"), namespace)
    return types.SimpleNamespace(**namespace)


def project_transcript(raw, walk_plan, qmap, programs):
    transcript = io.exact(io.parse(raw, "DLMF transcript"), {"schema", "records"}, "DLMF transcript")
    io.require(transcript["schema"] == observation.TRANSCRIPT_SCHEMA and raw == io.canonical(transcript), "invalid canonical DLMF transcript")
    state = observation.WalkState(walk_plan, programs["brain/ingest/dlmf.py"])
    for record in transcript["records"]:
        state.accept(record)
    facts, pending = state.result()
    io.require(pending is None, "DLMF walk is incomplete")
    parser = selected_parser(programs["brain/ingest/dlmf.py"], qmap)
    section_qids = parser.section_qids()
    pages = []
    for section in sorted(state.sections, key=lambda value: tuple(map(int, value.split(".")))):
        row = {"db": "dlmf", "id": section, "title": state.pages[section], "url": parser.BASE + section, "kind_hint": "section"}
        if section in section_qids:
            row["qid"] = section_qids[section]
        pages.append(row)
    rows = [{"db": "dlmf", "src": src, "dst": dst, "context": "body"} for src, dst in sorted(state.links)]
    io.require(len(pages) == facts["pages"] and len(rows) == facts["links"], "legacy projection differs from verified walk graph")
    return pages, rows, facts


def reduce_transcript(manifests, objects, captured, lineages, programs):
    raw = captured[("dlmf-page-walk", "page_transcript")]
    source = manifests["dlmf-page-walk"]
    obj = objects[("dlmf-page-walk", "page_transcript")]
    plan_raw = captured[("dlmf-page-walk", "normalization_plan")]
    walk_plan = io.parse(plan_raw, "original DLMF walk plan")
    io.require(source["source_kind"] == "acquired_dataset" and source["pin"] == {"type": "content_sha256", "value": io.sha(raw)} and
        obj["roles"] == ["normalized", "raw"] and obj["media_type"] == "application/json", "DLMF requires the reviewed transcript identity source")
    io.require(plan_raw == io.canonical(walk_plan) and io.sha(plan_raw) == lineages["dlmf-page-walk"]["configuration_sha256"],
        "original DLMF walk plan is not bound to parent normalization")
    pages, links, facts = project_transcript(raw, walk_plan, dlmf_qids(captured), programs)
    meta, pages, links = pairs.normalize_pair("dlmf", pages, links, {"source_pin": "sha256:" + io.sha(raw),
        "n_with_qid": sum("qid" in page for page in pages), "n_sections_enumerated": facts["sections_enumerated"]}, programs["brain/ingest/common.py"])
    io.require(all("snippet" not in page and "snippet_license" not in page for page in pages), "DLMF cannot emit article snippets")
    selected = {("dlmf-page-walk", "page_transcript"), ("wikidata-crossrefs", "wikidata_crossrefs"),
        ("wikidata-crossrefs", "requested_qid_scope"), (io.CURATED_SOURCE, "source_registry")}
    outputs = {"dlmf_" + kind: b"".join(io.artifact(row) + b"\n" for row in [{"_meta": meta}, *rows])
        for kind, rows in (("pages", pages), ("links", links))}
    outputs["dlmf_derivation"] = io.canonical({"schema": "wikilean.dlmf-derivation/v1", "transcript_sha256": io.sha(raw),
        "observation": "independent-live-requests/no-snapshot", "original_walk_plan_sha256": io.sha(plan_raw), "facts": facts,
        "source_manifest_id": source["source_manifest_id"], "crossref_source_manifest_id": manifests["wikidata-crossrefs"]["source_manifest_id"],
        "registry_source_manifest_id": manifests[io.CURATED_SOURCE]["source_manifest_id"], "snippets": "absent-from-normalized-graph; rawHTMLrestrictedprivate"})
    return outputs, selected


def build_documents(plan, sources, manifests, objects, captured, lineages, profile, programs, when):
    io.require(profile in profiles()["profiles"] and profile["files"] ==
        [{"path": path, "sha256": io.sha(raw)} for path, raw in sorted(programs.items())], "unreviewed DLMF program preimages")
    reduced = {"external-dlmf": reduce_transcript(manifests, objects, captured, lineages, programs)}
    files = {"plan.json": io.canonical(plan), "normalization/profile.json": io.canonical(profile),
        "normalization/configuration.json": io.canonical(CONFIGURATION),
        "normalization/upstream_walk_plan.json": captured[("dlmf-page-walk", "normalization_plan")]}
    files.update({"implementation/" + path: raw for path, raw in programs.items()})
    def planned(name, path, roles, media="application/json"):
        raw = files[path]
        item = {"name": name, "root": PHYSICAL_ROOT, "path": path, "sha256": io.sha(raw), "bytes": len(raw),
                "media_type": media, "roles": sorted(roles), "redistribution": "restricted"}
        files.setdefault("objects/sha256/" + item["sha256"], raw)
        return item
    tool = {"name": "wikilean-dlmf-normalizer", "version": "1", "sha256": io.sha(io.canonical(profile))}
    support = [planned("normalizer_profile", "normalization/profile.json", ["receipt"]),
        planned("normalizer_configuration", "normalization/configuration.json", ["receipt"]),
        planned("upstream_walk_plan", "normalization/upstream_walk_plan.json", ["receipt"])]
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
            path = "normalized/" + name + (".jsonl" if name.endswith(("_pages", "_links")) else ".json")
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
            "license": {"expression": "LicenseRef-DLMF-Review",
                "redistribution": "restricted", "notice": "Derived from explicitly reviewed private source evidence; publication is not approved."},
            "acquisition": tool, "normalization": {"schema": schema, "tool": tool,
                "inputs": sorted(item["name"] for item in raw_objects), "outputs": sorted(output)},
            "evidence": {"acquisition_receipts": [], "request_parameter_preimages": [], "normalization_lineage": {
                "root": PHYSICAL_ROOT, "path": path, "sha256": io.sha(files[path]), "bytes": len(files[path]),
                "media_type": "application/json", "normalization_lineage_id": lineage["normalization_lineage_id"]}}}
        manifest = io.source_plan_contracts._source_manifest_from_plan(child, "derived DLMF source")
        contracts.validate_source_manifest_evidence_documents(manifest, receipts={}, lineage=lineage,
            request_parameter_preimages={}, parent_source_manifests=parents)
        children.append(child)
        child_manifests.append(manifest)
        files["source-manifests/" + source + ".json"] = io.canonical(manifest)
    fragment = {"schema": "wikilean.dlmf-fragment/v1", "scope": "source-plan-fragment", "physical_root": PHYSICAL_ROOT,
        "source_publishable": False, "redistribution": "restricted", "sources": sorted([*sources.values(), *children], key=lambda source: source["source"]),
        "input_bindings": [{"input_id": "external-" + family, "state": "present", "sources": list(CHILDREN),
            "members": [{"path": db + "_" + family + ".jsonl", "source": "external-" + db, "object": db + "_" + family}
                        for db in ("dlmf",)]} for family in ("links", "pages")]}
    files["source-fragment.json"] = io.canonical(fragment)
    document = {"schema": EXPORT_SCHEMA, "normalization_profile_id": profile["profile_id"], "normalized_at": when,
        "source_manifest_ids": sorted(m["source_manifest_id"] for m in child_manifests),
        "files": {path: {"sha256": io.sha(raw), "bytes": len(raw)} for path, raw in sorted(files.items())}}
    document["export_id"] = contracts.domain_hash(EXPORT_SCHEMA, document)
    files["export.json"] = io.canonical(document)
    return files
