"""Pure ProofWiki XML projection over reviewed immutable source manifests.

The reviewed manifest IDs are the explicit trust root: upstream specialized
verifiers run before plan review. Every parent object and lineage is checked
here; only captured gzip bytes, scoped crossrefs and Git registry supply data.
"""
from __future__ import annotations

import ast
import gzip
import io as bytes_io
import xml.etree.ElementTree as ET
import re
import types
import urllib.parse
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

contracts = io.contracts
PLAN_SCHEMA = "wikilean.proofwiki-normalization-plan/v1"
EXPORT_SCHEMA = "wikilean.proofwiki-normalization-export/v1"
PROFILE_SCHEMA = "wikilean.proofwiki-normalization-profiles/v1"
REGISTRY = ROOT / "brain/proofwiki_source_profiles.json"
PHYSICAL_ROOT = "proofwiki_export"
PARENTS = {"proofwiki-dump", "wikidata-wbgetentities", "wikidata-crossrefs", io.CURATED_SOURCE}
CHILDREN = ("external-proofwiki",)
TOOL_FILES = tuple(sorted((set(io.TOOL_FILES) - {"brain/export_wikidata_crossrefs.py"}) |
    {*pairs.TOOL_FILES, "brain/proofwiki_sources.py", "brain/export_proofwiki_sources.py", "brain/ingest/proofwiki.py"}))


def origins():
    io.origins()
    pairs.origins()
    io.require(Path(pairs.__file__).resolve() == ROOT / "brain/external_pair_normalization.py", "pair helper origin differs")
    for module, path in ((common, "brain/ingest/common.py"), (build_context, "brain/build_context.py")):
        io.require(Path(getattr(module, "__file__", "")).resolve() == ROOT / path, "unexpected ProofWiki helper origin: " + path)
    io.require(common.seal_external_pair_meta is build_context.seal_external_pair_meta,
        "ProofWiki helpers use different imported dependencies")


origins()
LOADED = {path: io.sha(io.read(ROOT / path)) for path in TOOL_FILES if path != "brain/export_proofwiki_sources.py"}


def profile_id(profile):
    return contracts.domain_hash("wikilean.proofwiki-normalization-profile.v1", {"files": profile["files"]})


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
        io.require(paths == sorted(set(paths)) and "brain/proofwiki_sources.py" in paths and
            profile_id(profile) == profile["profile_id"], "invalid whole helper generation")
        ids.append(profile["profile_id"])
    io.require(ids == sorted(set(ids)) and value["current_profile"] in ids, "invalid current profile")
    return value


def current_profile():
    origins()
    value = profiles()
    profile = next(p for p in value["profiles"] if p["profile_id"] == value["current_profile"])
    actual = {path: io.sha(io.read(ROOT / path)) for path in TOOL_FILES}
    io.require(profile["files"] == [{"path": path, "sha256": actual[path]} for path in TOOL_FILES], "unreviewed current ProofWiki implementation")
    io.require(all(actual[path] == digest for path, digest in LOADED.items()), "loaded ProofWiki helper changed")
    return profile


def validate_plan(plan):
    io.exact(plan, {"schema", "parents", "reviewed_parent_manifest_ids"}, "ProofWiki plan")
    io.require(plan["schema"] == PLAN_SCHEMA and isinstance(plan["parents"], list) and
        [source["source"] for source in plan["parents"]] == sorted(PARENTS), "exact sorted ProofWiki parent closure required")
    io.require(isinstance(plan["reviewed_parent_manifest_ids"], dict) and set(plan["reviewed_parent_manifest_ids"]) == PARENTS,
        "every ProofWiki parent requires an explicitly reviewed manifest identity")
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
    manifests = {name: io.source_plan_contracts._source_manifest_from_plan(source, "ProofWiki parent") for name, source in sources.items()}
    io.require({name: m["source_manifest_id"] for name, m in manifests.items()} == plan["reviewed_parent_manifest_ids"],
        "parent differs from explicitly reviewed source manifest identity")
    by_id = {m["source_manifest_id"]: m for m in manifests.values()}
    objects = {(name, item["name"]): item for name, source in sources.items() for item in source["objects"]}
    selected = {(name, obj) for name, obj in objects if
        (name == "proofwiki-dump" and obj == "compressed_dump") or
        (name == "wikidata-crossrefs" and obj in {"wikidata_crossrefs", "requested_qid_scope"}) or
        (name == io.CURATED_SOURCE and obj == "source_registry")}
    captured = {}
    for name, source in sources.items():
        curated = None
        if source["source_kind"] == "curated_git_tree":
            io.require(name == io.CURATED_SOURCE and len(source["objects"]) == 1 and
                source["objects"][0]["path"] == io.REGISTRY_PATH, "unexpected curated ProofWiki parent selection")
            curated, tree, _proof, _tool = io.capture_git(roots[source["objects"][0]["root"]], source["pin"]["value"])
            io.require(tree == source["pin"]["tree"], "curated parent tree differs")
        for item in source["objects"]:
            raw = checked(curated if curated is not None else io.read(physical(item, roots)), item)
            key = (name, item["name"])
            if key in selected:
                io.require("normalized" in item["roles"], "ProofWiki input must be normalized")
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
        io.require(set(lineage["parent_source_manifest_ids"]) <= set(by_id), "missing ProofWiki parent evidence ancestor")
        contracts.validate_source_manifest_evidence_documents(manifests[name], receipts=receipts, lineage=lineage,
            request_parameter_preimages=preimages, parent_source_manifests={key: by_id[key] for key in lineage["parent_source_manifest_ids"]})
    return sources, manifests, objects, captured, lineages


PARSER_SYMBOLS = {"XNS", "KEEP_NS", "SKIP_PREFIXES", "WIKILINK_RE", "HEADING_RE", "ANY_LINK_RE", "PROOFISH_RE", "_WS",
    "norm_title", "out_of_scope", "unlink", "clean_wikitext", "iter_pages", "section_body", "extract_snippet"}
MAX_XML_BYTES = 4 * 1024 * 1024 * 1024
CONFIGURATION = {"schema": "wikilean.proofwiki-normalization/v1", "source": "exact captured gzip body; no XML cache",
    "maximum_decompressed_bytes_per_pass": MAX_XML_BYTES,
    "namespace_scope": ["0", "100", "102"], "parser_symbols": sorted(PARSER_SYMBOLS),
    "projection": "exact legacy main from redirects declaration through emit; gzip read from immutable bytes",
    "crossref_join": "explicit reviewed P6781 requested-QID scope; lowest numeric QID; existing redirect/collapse semantics",
    "pair_normalization": "exact common.emit prefix through validate_external_pair; no filesystem transaction",
    "publication": "restricted private evidence; existing snippet attribution retained without new permission"}


class BoundedXmlStream:
    """Count decompression again at the semantic boundary, for each XML pass."""
    def __init__(self, raw):
        self.stream = gzip.GzipFile(fileobj=bytes_io.BytesIO(raw), mode="rb")
        self.count = 0

    def read(self, size=-1):
        remaining = MAX_XML_BYTES - self.count
        data = self.stream.read(min(size, remaining + 1) if size >= 0 else remaining + 1)
        self.count += len(data)
        io.require(self.count <= MAX_XML_BYTES, "ProofWiki XML exceeds decompressed-byte bound")
        return data

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.stream.close()


def proofwiki_qids(captured):
    refs = io.parse(captured[("wikidata-crossrefs", "wikidata_crossrefs")], "crossrefs", data=True)
    scope = io.parse(captured[("wikidata-crossrefs", "requested_qid_scope")], "scope")
    io.exact(scope, {"schema", "qids"}, "scope")
    io.require(scope["schema"] == io.SCOPE_SCHEMA and isinstance(scope["qids"], list) and
        scope["qids"] == sorted(set(scope["qids"])) and set(refs["xrefs"]) <= set(scope["qids"]), "invalid requested crossref scope")
    io.require(all(isinstance(qid, str) and io.entities.QID_RE.fullmatch(qid) for qid in scope["qids"]), "invalid scope QID")
    io.require("proofwiki" in io.properties(captured[(io.CURATED_SOURCE, "source_registry")]).get("P6781", []) and
        "proofwiki" in refs["properties"].get("P6781", []), "ProofWiki identifiers require the curated P6781 mapping")
    qmap = {}
    for qid in sorted(refs["xrefs"], key=lambda q: (len(q), q)):
        values = refs["xrefs"][qid].get("proofwiki", [])
        io.require(isinstance(values, list) and all(isinstance(value, str) and value for value in values), "ProofWiki IDs must be concrete strings")
        for value in values:
            qmap.setdefault(value, qid)
    return qmap


def project_xml(raw, qmap, program):
    """Execute only explicit, reviewed legacy syntax over the supplied bytes."""
    tree = ast.parse(program, filename="sealed:brain/ingest/proofwiki.py")
    selected, names, mains = [], [], []
    for node in tree.body:
        name = node.name if isinstance(node, ast.FunctionDef) else (
            node.targets[0].id if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name) else None)
        if name in PARSER_SYMBOLS:
            selected.append(copy.deepcopy(node)); names.append(name)
        if name == "main": mains.append(node)
    io.require(set(names) == PARSER_SYMBOLS and len(names) == len(set(names)) and len(mains) == 1, "legacy ProofWiki parser selector differs")
    original = mains[0]
    starts = [i for i, node in enumerate(original.body) if isinstance(node, ast.AnnAssign) and
        isinstance(node.target, ast.Name) and node.target.id == "redirects"]
    io.require(len(starts) == 1, "legacy ProofWiki semantic block start differs")
    body = copy.deepcopy(original.body[starts[0]:])
    last = body[-1]
    io.require(isinstance(last, ast.Expr) and isinstance(last.value, ast.Call) and isinstance(last.value.func, ast.Attribute) and
        isinstance(last.value.func.value, ast.Name) and last.value.func.value.id == "common" and last.value.func.attr == "emit",
        "legacy ProofWiki semantic block end differs")
    body[-1] = ast.Return(value=last.value)
    selected.append(ast.FunctionDef(name="project", args=ast.arguments(posonlyargs=[], args=[ast.arg(arg="dump"), ast.arg(arg="dump_pin")],
        kwonlyargs=[], kw_defaults=[], defaults=[]), body=body, decorator_list=[]))
    def memory_open(value, mode):
        io.require(value is raw and mode == "rb", "XML parser attempted an ambient gzip read")
        return BoundedXmlStream(raw)
    def emit(db, pages, links, meta):
        io.require(db == "proofwiki", "unexpected output family")
        return pages, links, meta
    def content_pin(value):
        io.require(value is raw, "XML parser attempted an ambient content pin")
        return "sha256:" + io.sha(raw)
    def qids(db):
        io.require(db == "proofwiki", "unexpected crossref family")
        return dict(qmap)
    namespace = {"re": re, "urllib": urllib, "ET": ET, "gzip": types.SimpleNamespace(open=memory_open),
        "common": types.SimpleNamespace(qid_map=qids, content_sha256_pin=content_pin, emit=emit)}
    module = ast.Module(body=[ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0), *selected], type_ignores=[])
    exec(compile(ast.fix_missing_locations(module), "sealed:proofwiki-projection", "exec"), namespace)
    return namespace["project"](raw, content_pin(raw))


def reduce_xml(manifests, objects, captured, programs):
    raw = captured[("proofwiki-dump", "compressed_dump")]
    source = manifests["proofwiki-dump"]
    obj = objects[("proofwiki-dump", "compressed_dump")]
    io.require(source["source_kind"] == "acquired_dataset" and source["pin"] == {"type": "content_sha256", "value": io.sha(raw)} and
        obj["roles"] == ["normalized", "raw"] and obj["media_type"] == "application/gzip", "ProofWiki dump requires the reviewed compressed identity source")
    pages, links, extra = project_xml(raw, proofwiki_qids(captured), programs["brain/ingest/proofwiki.py"])
    meta, pages, links = pairs.normalize_pair("proofwiki", pages, links, extra, programs["brain/ingest/common.py"])
    selected = {("proofwiki-dump", "compressed_dump"), ("wikidata-crossrefs", "wikidata_crossrefs"),
        ("wikidata-crossrefs", "requested_qid_scope"), (io.CURATED_SOURCE, "source_registry")}
    outputs = {"proofwiki_" + kind: b"".join(io.artifact(row) + b"\n" for row in [{"_meta": meta}, *rows])
        for kind, rows in (("pages", pages), ("links", links))}
    outputs["proofwiki_derivation"] = io.canonical({"schema": "wikilean.proofwiki-derivation/v1", "compressed_sha256": io.sha(raw),
        "source_manifest_id": source["source_manifest_id"], "crossref_source_manifest_id": manifests["wikidata-crossrefs"]["source_manifest_id"],
        "registry_source_manifest_id": manifests[io.CURATED_SOURCE]["source_manifest_id"], "metadata": meta,
        "snippet_permission": "existing attribution retained; restricted private use; no new permission asserted"})
    return outputs, selected



def build_documents(plan, sources, manifests, objects, captured, lineages, profile, programs, when):
    io.require(profile in profiles()["profiles"] and profile["files"] ==
        [{"path": path, "sha256": io.sha(raw)} for path, raw in sorted(programs.items())], "unreviewed ProofWiki program preimages")
    reduced = {"external-proofwiki": reduce_xml(manifests, objects, captured, programs)}
    files = {"plan.json": io.canonical(plan), "normalization/profile.json": io.canonical(profile),
        "normalization/configuration.json": io.canonical(CONFIGURATION)}
    files.update({"implementation/" + path: raw for path, raw in programs.items()})
    def planned(name, path, roles, media="application/json"):
        raw = files[path]
        item = {"name": name, "root": PHYSICAL_ROOT, "path": path, "sha256": io.sha(raw), "bytes": len(raw),
                "media_type": media, "roles": sorted(roles), "redistribution": "restricted"}
        files.setdefault("objects/sha256/" + item["sha256"], raw)
        return item
    tool = {"name": "wikilean-proofwiki-normalizer", "version": "1", "sha256": io.sha(io.canonical(profile))}
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
            "license": {"expression": "LicenseRef-ProofWiki-Review",
                "redistribution": "restricted", "notice": "Derived from explicitly reviewed private source evidence; publication is not approved."},
            "acquisition": tool, "normalization": {"schema": schema, "tool": tool,
                "inputs": sorted(item["name"] for item in raw_objects), "outputs": sorted(output)},
            "evidence": {"acquisition_receipts": [], "request_parameter_preimages": [], "normalization_lineage": {
                "root": PHYSICAL_ROOT, "path": path, "sha256": io.sha(files[path]), "bytes": len(files[path]),
                "media_type": "application/json", "normalization_lineage_id": lineage["normalization_lineage_id"]}}}
        manifest = io.source_plan_contracts._source_manifest_from_plan(child, "derived ProofWiki source")
        contracts.validate_source_manifest_evidence_documents(manifest, receipts={}, lineage=lineage,
            request_parameter_preimages={}, parent_source_manifests=parents)
        children.append(child)
        child_manifests.append(manifest)
        files["source-manifests/" + source + ".json"] = io.canonical(manifest)
    fragment = {"schema": "wikilean.proofwiki-fragment/v1", "scope": "source-plan-fragment", "physical_root": PHYSICAL_ROOT,
        "source_publishable": False, "redistribution": "restricted", "sources": sorted([*sources.values(), *children], key=lambda source: source["source"]),
        "input_bindings": [{"input_id": "external-" + family, "state": "present", "sources": list(CHILDREN),
            "members": [{"path": db + "_" + family + ".jsonl", "source": "external-" + db, "object": db + "_" + family}
                        for db in ("proofwiki",)]} for family in ("links", "pages")]}
    files["source-fragment.json"] = io.canonical(fragment)
    document = {"schema": EXPORT_SCHEMA, "normalization_profile_id": profile["profile_id"], "normalized_at": when,
        "source_manifest_ids": sorted(m["source_manifest_id"] for m in child_manifests),
        "files": {path: {"sha256": io.sha(raw), "bytes": len(raw)} for path, raw in sorted(files.items())}}
    document["export_id"] = contracts.domain_hash(EXPORT_SCHEMA, document)
    files["export.json"] = io.canonical(document)
    return files
