"""Pure LMFDB knowl projection over reviewed immutable source manifests.

The reviewed manifest IDs are the explicit trust root: upstream specialized
verifiers run before plan review. Every parent object and lineage is checked
here; only captured query response bytes, scoped crossrefs and Git registry supply data.
"""
from __future__ import annotations

import ast
import datetime as dt
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
import lmfdb_source_evidence as observation

contracts = io.contracts
PLAN_SCHEMA = "wikilean.lmfdb-normalization-plan/v1"
EXPORT_SCHEMA = "wikilean.lmfdb-normalization-export/v1"
PROFILE_SCHEMA = "wikilean.lmfdb-normalization-profiles/v1"
REGISTRY = ROOT / "brain/lmfdb_normalization_profiles.json"
PHYSICAL_ROOT = "lmfdb_export"
PARENTS = {"lmfdb-knowl-observation", "wikidata-wbgetentities", "wikidata-crossrefs", io.CURATED_SOURCE}
CHILDREN = ("external-lmfdb-knowl",)
TOOL_FILES = tuple(sorted((set(io.TOOL_FILES) - {"brain/export_wikidata_crossrefs.py"}) |
    {*pairs.TOOL_FILES, "brain/lmfdb_normalization.py", "brain/export_lmfdb_normalization.py", "brain/ingest/lmfdb.py", "brain/lmfdb_source_evidence.py", "brain/mathlib_source_evidence.py"}))


def origins():
    io.origins()
    observation.origins()
    io.require(Path(observation.__file__).resolve() == ROOT / "brain/lmfdb_source_evidence.py" and
        observation.contracts is contracts, "LMFDB replay helper origin or contract differs")
    pairs.origins()
    io.require(Path(pairs.__file__).resolve() == ROOT / "brain/external_pair_normalization.py", "pair helper origin differs")
    for module, path in ((common, "brain/ingest/common.py"), (build_context, "brain/build_context.py")):
        io.require(Path(getattr(module, "__file__", "")).resolve() == ROOT / path, "unexpected LMFDB helper origin: " + path)
    io.require(common.seal_external_pair_meta is build_context.seal_external_pair_meta,
        "LMFDB helpers use different imported dependencies")


origins()
LOADED = {path: io.sha(io.read(ROOT / path)) for path in TOOL_FILES if path != "brain/export_lmfdb_normalization.py"}


def profile_id(profile):
    return contracts.domain_hash("wikilean.lmfdb-normalization-profile.v1", {"files": profile["files"]})


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
        io.require(paths == sorted(set(paths)) and "brain/lmfdb_normalization.py" in paths and
            profile_id(profile) == profile["profile_id"], "invalid whole helper generation")
        ids.append(profile["profile_id"])
    io.require(ids == sorted(set(ids)) and value["current_profile"] in ids, "invalid current profile")
    return value


def current_profile():
    origins()
    value = profiles()
    profile = next(p for p in value["profiles"] if p["profile_id"] == value["current_profile"])
    actual = {path: io.sha(io.read(ROOT / path)) for path in TOOL_FILES}
    io.require(profile["files"] == [{"path": path, "sha256": actual[path]} for path in TOOL_FILES], "unreviewed current LMFDB implementation")
    io.require(all(actual[path] == digest for path, digest in LOADED.items()), "loaded LMFDB helper changed")
    return profile


def validate_plan(plan):
    io.exact(plan, {"schema", "parents", "reviewed_parent_manifest_ids"}, "LMFDB plan")
    io.require(plan["schema"] == PLAN_SCHEMA and isinstance(plan["parents"], list) and
        [source["source"] for source in plan["parents"]] == sorted(PARENTS), "exact sorted LMFDB parent closure required")
    io.require(isinstance(plan["reviewed_parent_manifest_ids"], dict) and set(plan["reviewed_parent_manifest_ids"]) == PARENTS,
        "every LMFDB parent requires an explicitly reviewed manifest identity")
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
    manifests = {name: io.source_plan_contracts._source_manifest_from_plan(source, "LMFDB parent") for name, source in sources.items()}
    io.require({name: m["source_manifest_id"] for name, m in manifests.items()} == plan["reviewed_parent_manifest_ids"],
        "parent differs from explicitly reviewed source manifest identity")
    by_id = {m["source_manifest_id"]: m for m in manifests.values()}
    objects = {(name, item["name"]): item for name, source in sources.items() for item in source["objects"]}
    selected = {(name, obj) for name, obj in objects if
        (name == "lmfdb-knowl-observation" and obj in {"knowl_query_response", "normalization_plan", "acquisition_transport", "acquisition_peer_certificate"}) or
        (name == "wikidata-crossrefs" and obj in {"wikidata_crossrefs", "requested_qid_scope"}) or
        (name == io.CURATED_SOURCE and obj == "source_registry")}
    captured = {}
    for name, source in sources.items():
        curated = None
        if source["source_kind"] == "curated_git_tree":
            io.require(name == io.CURATED_SOURCE and len(source["objects"]) == 1 and
                source["objects"][0]["path"] == io.REGISTRY_PATH, "unexpected curated LMFDB parent selection")
            curated, tree, _proof, _tool = io.capture_git(roots[source["objects"][0]["root"]], source["pin"]["value"])
            io.require(tree == source["pin"]["tree"], "curated parent tree differs")
        for item in source["objects"]:
            raw = checked(curated if curated is not None else io.read(physical(item, roots)), item)
            key = (name, item["name"])
            if key in selected:
                io.require("normalized" in item["roles"] or (name == "lmfdb-knowl-observation" and item["name"] in {"normalization_plan", "acquisition_transport", "acquisition_peer_certificate"} and item["roles"] == ["receipt"]), "LMFDB input must be normalized or bound observation support")
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
        io.require(set(lineage["parent_source_manifest_ids"]) <= set(by_id), "missing LMFDB parent evidence ancestor")
        contracts.validate_source_manifest_evidence_documents(manifests[name], receipts=receipts, lineage=lineage,
            request_parameter_preimages=preimages, parent_source_manifests={key: by_id[key] for key in lineage["parent_source_manifest_ids"]})
    return sources, manifests, objects, captured, lineages


PARSER_SYMBOLS = {"URL", "BRACE", "ARG", "WIKIDATA_KW", "template_args", "strip_templates"}
CONFIGURATION = {"schema": "wikilean.lmfdb-normalization/v1", "source": "exact bounded read-only repeatable-read knowl query and original bound plan/transport/certificate",
    "replay": "original response identity, certificate pin, read-only transaction, schema/latest-revision/count/byte checks",
    "projection": "exact legacy main after connection closure; original query row order and ISO timestamp decoding; template helpers unchanged",
    "parser_symbols": sorted(PARSER_SYMBOLS), "crossref_join": "reviewed P12987 requested-QID scope; first full-content wikidata= match wins except doc.* uses only crossref fallback",
    "pair_normalization": "exact common.emit pure prefix through validation; content-addressed source pin",
    "publication": "restricted private source evidence; legacy CC-BY-SA-4.0 snippets; no new permission"}


def lmfdb_qids(captured):
    refs = io.parse(captured[("wikidata-crossrefs", "wikidata_crossrefs")], "crossrefs", data=True)
    scope = io.parse(captured[("wikidata-crossrefs", "requested_qid_scope")], "scope")
    io.exact(scope, {"schema", "qids"}, "scope")
    io.require(scope["schema"] == io.SCOPE_SCHEMA and isinstance(scope["qids"], list) and
        scope["qids"] == sorted(set(scope["qids"])) and set(refs["xrefs"]) <= set(scope["qids"]), "invalid requested crossref scope")
    io.require(all(isinstance(qid, str) and io.entities.QID_RE.fullmatch(qid) for qid in scope["qids"]), "invalid scope QID")
    io.require("lmfdb_knowl" in io.properties(captured[(io.CURATED_SOURCE, "source_registry")]).get("P12987", []) and
        "lmfdb_knowl" in refs["properties"].get("P12987", []), "LMFDB identifiers require the curated P12987 mapping")
    qmap = {}
    for qid in sorted(refs["xrefs"], key=lambda q: (len(q), q)):
        values = refs["xrefs"][qid].get("lmfdb_knowl", [])
        io.require(isinstance(values, list) and all(isinstance(value, str) and value for value in values), "LMFDB IDs must be concrete strings")
        for value in values:
            qmap.setdefault(value, qid)
    return qmap


def selected_projection(program, rows, qmap):
    tree = ast.parse(program, filename="sealed:brain/ingest/lmfdb.py")
    selected, names, mains = [], [], []
    for node in tree.body:
        name = node.name if isinstance(node, ast.FunctionDef) else (
            node.targets[0].id if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name) else None)
        if name in PARSER_SYMBOLS:
            selected.append(node); names.append(name)
        if name == "main": mains.append(node)
    io.require(set(names) == PARSER_SYMBOLS and len(names) == len(set(names)) and len(mains) == 1, "legacy LMFDB parser selector differs")
    body = mains[0].body
    starts = [i for i, node in enumerate(body) if isinstance(node, ast.Assign) and len(node.targets) == 1 and
        isinstance(node.targets[0], ast.Name) and node.targets[0].id == "qids"]
    stops = [i for i, node in enumerate(body) if isinstance(node, ast.Expr) and isinstance(node.value, ast.Call) and
        isinstance(node.value.func, ast.Attribute) and isinstance(node.value.func.value, ast.Name) and
        node.value.func.value.id == "common" and node.value.func.attr == "emit"]
    io.require(len(starts) == len(stops) == 1 and starts[0] < stops[0], "legacy LMFDB main selector differs")
    statements = body[starts[0]:stops[0]]
    io.require(isinstance(statements[-1], ast.Assign) and len(statements[-1].targets) == 1 and
        isinstance(statements[-1].targets[0], ast.Name) and statements[-1].targets[0].id == "link_rows", "legacy LMFDB projection boundary differs")
    module = ast.Module(body=[ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0), *selected, *statements], type_ignores=[])
    def qids(db):
        io.require(db == "lmfdb_knowl", "unexpected crossref family")
        return dict(qmap)
    namespace = {"re": re, "rows": rows, "common": types.SimpleNamespace(qid_map=qids)}
    exec(compile(ast.fix_missing_locations(module), "sealed:lmfdb-projection", "exec"), namespace)
    return namespace["pages"], namespace["link_rows"]


def project_response(raw, upstream_plan, transport, certificate, qmap, programs):
    data = observation.normalize(upstream_plan, raw, transport, certificate)
    rows = [(row["id"], row["title"], row["content"], row["links"],
        dt.datetime.fromisoformat(row["timestamp"]) if row["timestamp"] is not None else None) for row in data["rows"]]
    pages, links = selected_projection(programs["brain/ingest/lmfdb.py"], rows, qmap)
    io.require(len(pages) == data["row_count"], "legacy projection differs from verified query row count")
    facts = {"query_rows": data["row_count"], "pages": len(pages), "links": len(links),
        "read_only": data["read_only"], "isolation": data["isolation"], "snapshot": data["snapshot"],
        "response_bytes": len(raw), "with_qid": sum("qid" in row for row in pages),
        "with_snippet": sum("snippet" in row for row in pages)}
    return pages, links, facts


def reduce_response(manifests, objects, captured, lineages, programs):
    raw = captured[("lmfdb-knowl-observation", "knowl_query_response")]
    source = manifests["lmfdb-knowl-observation"]
    obj = objects[("lmfdb-knowl-observation", "knowl_query_response")]
    plan_raw = captured[("lmfdb-knowl-observation", "normalization_plan")]
    upstream_plan = io.parse(plan_raw, "original LMFDB query plan")
    transport = io.parse(captured[("lmfdb-knowl-observation", "acquisition_transport")], "original LMFDB transport")
    certificate = captured[("lmfdb-knowl-observation", "acquisition_peer_certificate")]
    io.require(source["source_kind"] == "acquired_dataset" and source["pin"] == {"type": "content_sha256", "value": io.sha(raw)} and
        obj["roles"] == ["normalized", "raw"] and obj["media_type"] == "application/json", "LMFDB requires the reviewed query identity source")
    io.require(plan_raw == io.canonical(upstream_plan) and io.sha(plan_raw) == lineages["lmfdb-knowl-observation"]["configuration_sha256"],
        "original LMFDB query plan is not bound to parent normalization")
    pages, links, facts = project_response(raw, upstream_plan, transport, certificate, lmfdb_qids(captured), programs)
    meta, pages, links = pairs.normalize_pair("lmfdb_knowl", pages, links, {"source_pin": "sha256:" + io.sha(raw),
        "n_with_qid": sum("qid" in page for page in pages)}, programs["brain/ingest/common.py"])
    selected = {("lmfdb-knowl-observation", "knowl_query_response"), ("wikidata-crossrefs", "wikidata_crossrefs"),
        ("wikidata-crossrefs", "requested_qid_scope"), (io.CURATED_SOURCE, "source_registry")}
    outputs = {"lmfdb_knowl_" + kind: b"".join(io.artifact(row) + b"\n" for row in [{"_meta": meta}, *rows])
        for kind, rows in (("pages", pages), ("links", links))}
    outputs["lmfdb_knowl_derivation"] = io.canonical({"schema": "wikilean.lmfdb-derivation/v1", "response_sha256": io.sha(raw),
        "original_query_plan_sha256": io.sha(plan_raw), "facts": facts,
        "source_manifest_id": source["source_manifest_id"], "crossref_source_manifest_id": manifests["wikidata-crossrefs"]["source_manifest_id"],
        "registry_source_manifest_id": manifests[io.CURATED_SOURCE]["source_manifest_id"]})
    return outputs, selected


def build_documents(plan, sources, manifests, objects, captured, lineages, profile, programs, when):
    io.require(profile in profiles()["profiles"] and profile["files"] ==
        [{"path": path, "sha256": io.sha(raw)} for path, raw in sorted(programs.items())], "unreviewed LMFDB program preimages")
    reduced = {CHILDREN[0]: reduce_response(manifests, objects, captured, lineages, programs)}
    files = {"plan.json": io.canonical(plan), "normalization/profile.json": io.canonical(profile),
        "normalization/configuration.json": io.canonical(CONFIGURATION),
        "normalization/upstream_query_plan.json": captured[("lmfdb-knowl-observation", "normalization_plan")],
        "normalization/upstream_transport.json": captured[("lmfdb-knowl-observation", "acquisition_transport")],
        "normalization/upstream_peer_certificate.der": captured[("lmfdb-knowl-observation", "acquisition_peer_certificate")]}
    files.update({"implementation/" + path: raw for path, raw in programs.items()})
    def planned(name, path, roles, media="application/json"):
        raw = files[path]
        item = {"name": name, "root": PHYSICAL_ROOT, "path": path, "sha256": io.sha(raw), "bytes": len(raw),
                "media_type": media, "roles": sorted(roles), "redistribution": "restricted"}
        files.setdefault("objects/sha256/" + item["sha256"], raw)
        return item
    tool = {"name": "wikilean-lmfdb-normalizer", "version": "1", "sha256": io.sha(io.canonical(profile))}
    support = [planned("normalizer_profile", "normalization/profile.json", ["receipt"]),
        planned("normalizer_configuration", "normalization/configuration.json", ["receipt"]),
        planned("upstream_query_plan", "normalization/upstream_query_plan.json", ["receipt"]),
        planned("upstream_transport", "normalization/upstream_transport.json", ["receipt"]),
        planned("upstream_peer_certificate", "normalization/upstream_peer_certificate.der", ["receipt"], "application/pkix-cert")]
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
            "license": {"expression": "CC-BY-SA-4.0",
                "redistribution": "restricted", "notice": "Derived from explicitly reviewed private source evidence; publication is not approved."},
            "acquisition": tool, "normalization": {"schema": schema, "tool": tool,
                "inputs": sorted(item["name"] for item in raw_objects), "outputs": sorted(output)},
            "evidence": {"acquisition_receipts": [], "request_parameter_preimages": [], "normalization_lineage": {
                "root": PHYSICAL_ROOT, "path": path, "sha256": io.sha(files[path]), "bytes": len(files[path]),
                "media_type": "application/json", "normalization_lineage_id": lineage["normalization_lineage_id"]}}}
        manifest = io.source_plan_contracts._source_manifest_from_plan(child, "derived LMFDB source")
        contracts.validate_source_manifest_evidence_documents(manifest, receipts={}, lineage=lineage,
            request_parameter_preimages={}, parent_source_manifests=parents)
        children.append(child)
        child_manifests.append(manifest)
        files["source-manifests/" + source + ".json"] = io.canonical(manifest)
    fragment = {"schema": "wikilean.lmfdb-fragment/v1", "scope": "source-plan-fragment", "physical_root": PHYSICAL_ROOT,
        "source_publishable": False, "redistribution": "restricted", "sources": sorted([*sources.values(), *children], key=lambda source: source["source"]),
        "input_bindings": [{"input_id": "external-" + family, "state": "present", "sources": list(CHILDREN),
            "members": [{"path": db + "_" + family + ".jsonl", "source": CHILDREN[0], "object": db + "_" + family}
                        for db in ("lmfdb_knowl",)]} for family in ("links", "pages")]}
    files["source-fragment.json"] = io.canonical(fragment)
    document = {"schema": EXPORT_SCHEMA, "normalization_profile_id": profile["profile_id"], "normalized_at": when,
        "source_manifest_ids": sorted(m["source_manifest_id"] for m in child_manifests),
        "files": {path: {"sha256": io.sha(raw), "bytes": len(raw)} for path, raw in sorted(files.items())}}
    document["export_id"] = contracts.domain_hash(EXPORT_SCHEMA, document)
    files["export.json"] = io.canonical(document)
    return files
