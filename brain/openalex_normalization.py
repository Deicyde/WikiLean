"""Pure OpenAlex structure/content projection over reviewed immutable source manifests.

The reviewed manifest IDs are the explicit trust root: upstream specialized
verifiers run before plan review. Every parent object and lineage is checked
here; only captured API response bytes, scoped crossrefs and Git registry supply data.
"""
from __future__ import annotations

import copy
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
for directory in (ROOT / "brain", ROOT / "brain/ingest", ROOT / "catalog"):
    sys.path.append(str(directory))
import wikidata_crossref_sources as io
import openalex_source_evidence as observation

contracts = io.contracts
PLAN_SCHEMA = "wikilean.openalex-normalization-plan/v1"
EXPORT_SCHEMA = "wikilean.openalex-normalization-export/v1"
PROFILE_SCHEMA = "wikilean.openalex-normalization-profiles/v1"
REGISTRY = ROOT / "brain/openalex_normalization_profiles.json"
PHYSICAL_ROOT = "openalex_export"
REQUIRED_PARENTS = {observation.SOURCE, observation.SCOPE_SOURCE}
CHILDREN = ("external-arxiv-citations",)
TOOL_FILES = tuple(sorted((set(io.TOOL_FILES) - {"brain/export_wikidata_crossrefs.py"}) |
    {"brain/openalex_normalization.py", "brain/export_openalex_normalization.py", "brain/ingest/openalex_citations.py", "brain/openalex_source_evidence.py", "brain/mathlib_source_evidence.py"}))


def origins():
    io.origins(); observation.origins()
    io.require(Path(observation.__file__).resolve() == ROOT / "brain/openalex_source_evidence.py" and observation.contracts is contracts,
        "OpenAlex replay helper origin differs")


origins()
LOADED = {path: io.sha(io.read(ROOT / path)) for path in TOOL_FILES if path != "brain/export_openalex_normalization.py"}


def profile_id(profile):
    return contracts.domain_hash("wikilean.openalex-normalization-profile.v1", {"files": profile["files"]})


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
        io.require(paths == sorted(set(paths)) and "brain/openalex_normalization.py" in paths and
            profile_id(profile) == profile["profile_id"], "invalid whole helper generation")
        ids.append(profile["profile_id"])
    io.require(ids == sorted(set(ids)) and value["current_profile"] in ids, "invalid current profile")
    return value


def current_profile():
    origins()
    value = profiles()
    profile = next(p for p in value["profiles"] if p["profile_id"] == value["current_profile"])
    actual = {path: io.sha(io.read(ROOT / path)) for path in TOOL_FILES}
    io.require(profile["files"] == [{"path": path, "sha256": actual[path]} for path in TOOL_FILES], "unreviewed current OpenAlex implementation")
    io.require(all(actual[path] == digest for path, digest in LOADED.items()), "loaded OpenAlex helper changed")
    return profile


def validate_plan(plan):
    io.exact(plan, {"schema", "parents", "reviewed_parent_manifest_ids"}, "OpenAlex plan")
    io.require(plan["schema"] == PLAN_SCHEMA and isinstance(plan["parents"], list) and len(plan["parents"]) <= 100, "invalid OpenAlex parent plan")
    names = [s["source"] for s in plan["parents"]]
    io.require(names == sorted(set(names)) and REQUIRED_PARENTS <= set(names), "exact sorted OpenAlex and scope parents required")
    io.require(isinstance(plan["reviewed_parent_manifest_ids"], dict) and set(plan["reviewed_parent_manifest_ids"]) == set(names), "every parent needs an explicitly reviewed identity")
    for value in plan["reviewed_parent_manifest_ids"].values(): contracts._hash(value, "reviewed parent")
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


def curated_objects(source, roots):
    git = io.git_snapshot
    command = git._validated_git("/usr/bin/git")
    digest = io.sha(io.read(Path(command), executable=True))
    root_names = {obj["root"] for obj in source["objects"]}
    io.require(len(root_names) == 1, "curated ancestor requires one native Git root")
    repository = git._validated_repository(roots[next(iter(root_names))])
    git._require_top_level(command, repository); git._reject_partial_clone(command, repository)
    commit = source["pin"]["value"]
    io.require(git._run_git(command, repository, ["cat-file", "-t", commit]) == b"commit\n" and
        git._commit_tree(command, repository, commit) == source["pin"]["tree"], "curated ancestor commit/tree differs")
    result = {}
    for item in source["objects"]:
        entries = git._select_entries(git._tree_entries(command, repository, commit, item["path"]), item["path"], None)
        values = git._read_text_blobs(command, repository, entries)
        io.require(len(values) == 1 and values[0].path == item["path"], "curated ancestor path differs")
        result[item["name"]] = checked(values[0].text.encode("utf-8"), item)
    io.require(io.sha(io.read(Path(command), executable=True)) == digest, "Git reader changed")
    return result


def capture_parents(plan, roots):
    validate_plan(plan)
    sources = {s["source"]: copy.deepcopy(s) for s in plan["parents"]}
    manifests = {name: io.source_plan_contracts._source_manifest_from_plan(source, "OpenAlex parent") for name, source in sources.items()}
    io.require({name: m["source_manifest_id"] for name, m in manifests.items()} == plan["reviewed_parent_manifest_ids"], "parent differs from explicitly reviewed identity")
    by_id = {m["source_manifest_id"]: m for m in manifests.values()}
    objects = {(name, item["name"]): item for name, source in sources.items() for item in source["objects"]}
    selected = {(observation.SOURCE, "citation_transcript"), (observation.SOURCE, "normalization_plan"),
        (observation.SOURCE, "theoremgraph_selector"), (observation.SCOPE_SOURCE, observation.SCOPE_OBJECT)}
    io.require(selected <= set(objects), "OpenAlex scope or transcript support is absent")
    captured, lineages = {}, {}
    for name, source in sources.items():
        curated = curated_objects(source, roots) if source["source_kind"] == "curated_git_tree" else None
        for item in source["objects"]:
            raw = checked(curated[item["name"]] if curated is not None else io.read(physical(item, roots)), item)
            key = (name, item["name"])
            if key in selected:
                io.require("normalized" in item["roles"] or (name == observation.SOURCE and item["name"] in {"normalization_plan", "theoremgraph_selector"} and item["roles"] == ["receipt"]), "invalid OpenAlex parent input role")
                captured[key] = raw
        if curated is not None: continue
        evidence, receipts, preimages = source["evidence"], {}, {}
        for ref in evidence["acquisition_receipts"]:
            receipts[ref["acquisition_receipt_id"]] = io.parse(checked(io.read(physical(ref, roots)), ref), "parent receipt")
        ref = evidence["normalization_lineage"]
        lineage = io.parse(checked(io.read(physical(ref, roots)), ref), "parent lineage"); lineages[name] = lineage
        for ref in evidence["request_parameter_preimages"]:
            checked(io.read(physical(ref, roots)), ref)
            preimages[ref["parameters_sha256"]] = {key: ref[key] for key in ("parameters_sha256", "bytes", "media_type")}
        io.require(set(lineage["parent_source_manifest_ids"]) <= set(by_id), "missing OpenAlex parent evidence ancestor")
        contracts.validate_source_manifest_evidence_documents(manifests[name], receipts=receipts, lineage=lineage,
            request_parameter_preimages=preimages, parent_source_manifests={key: by_id[key] for key in lineage["parent_source_manifest_ids"]})
    pending = [manifests[name]["source_manifest_id"] for name in REQUIRED_PARENTS]
    seen = set()
    while pending:
        identity = pending.pop()
        if identity in seen: continue
        seen.add(identity); source = by_id[identity]
        if source["source"] in lineages: pending.extend(lineages[source["source"]]["parent_source_manifest_ids"])
    io.require(seen == set(by_id), "OpenAlex parent plan contains unrelated source authority")
    return sources, manifests, objects, captured, lineages


CONFIGURATION = {"schema": "wikilean.openalex-normalization/v1", "source": "complete original scoped OpenAlex/arXiv transcript",
    "observation": "independent-live-requests/no-snapshot", "projection": "exact legacy arxiv_of_work/doi_of and one-round A/A2/Jwork/B/C/final-B citation fold",
    "scope": "exact reviewed theoremgraph-links parent object, original selector bytes, all included and excluded identifiers",
    "negative": "only explicit direct 404 or complete filter absence; malformed/partial/quota/auth responses never become missing records",
    "publication": "restricted private evidence; public metadata CC0; no article full text or snippets"}


def reduce_transcript(manifests, objects, captured, lineages, programs):
    raw = captured[(observation.SOURCE, "citation_transcript")]
    source = manifests[observation.SOURCE]
    obj = objects[(observation.SOURCE, "citation_transcript")]
    plan_raw = captured[(observation.SOURCE, "normalization_plan")]
    upstream_plan = io.parse(plan_raw, "original OpenAlex request plan")
    io.require(plan_raw == io.canonical(upstream_plan) and io.sha(plan_raw) == lineages[observation.SOURCE]["configuration_sha256"], "original OpenAlex plan is not bound to parent normalization")
    scope = manifests[observation.SCOPE_SOURCE]
    selector_obj = objects[(observation.SCOPE_SOURCE, observation.SCOPE_OBJECT)]
    selector = captured[(observation.SCOPE_SOURCE, observation.SCOPE_OBJECT)]
    io.require(scope["source_kind"] == "sealed_snapshot" and upstream_plan["scope"] == {
        "source": observation.SCOPE_SOURCE, "source_manifest_id": scope["source_manifest_id"], "object": observation.SCOPE_OBJECT,
        "sha256": selector_obj["sha256"], "bytes": selector_obj["bytes"]}, "OpenAlex request scope differs from exact reviewed theoremgraph generation")
    io.require(captured[(observation.SOURCE, "theoremgraph_selector")] == selector, "OpenAlex retained selector differs from normalized theoremgraph parent")
    observation.validate_selector(upstream_plan, selector, programs["brain/ingest/openalex_citations.py"])
    io.require(source["source_kind"] == "acquired_dataset" and source["pin"] == {"type": "content_sha256", "value": io.sha(raw)} and
        obj["roles"] == ["normalized", "raw"] and obj["media_type"] == "application/json", "OpenAlex requires reviewed complete transcript identity")
    transcript = io.exact(io.parse(raw, "transcript"), {"schema", "records"}, "transcript")
    io.require(transcript["schema"] == observation.TRANSCRIPT_SCHEMA and raw == io.canonical(transcript), "invalid canonical OpenAlex transcript")
    state = observation.WalkState(upstream_plan, programs["brain/ingest/openalex_citations.py"])
    for row in transcript["records"]: state.accept(row)
    facts = state.facts(); rows = state.result["rows"]
    meta = {"db": "openalex", "source_pin": "sha256:" + io.sha(raw), "license": "CC0 (OpenAlex)",
        "meaning": "src's bibliography cites dst; both endpoints restricted to the theoremgraph_links.json arXiv id set",
        "n_arxiv_ids": facts["scope_ids"], "n_skipped_non_arxiv": facts["excluded_ids"], "n_links": len(rows)}
    outputs = {"arxiv_citations": b"".join(io.artifact(row) + b"\n" for row in [{"_meta": meta}, *rows]),
        "arxiv_citations_derivation": io.canonical({"schema": "wikilean.openalex-citation-derivation/v1", "facts": facts,
            "observation_source_manifest_id": source["source_manifest_id"], "scope_source_manifest_id": scope["source_manifest_id"],
            "original_request_plan_sha256": io.sha(plan_raw), "arxiv_ids": upstream_plan["arxiv_ids"],
            "excluded_non_arxiv_ids": upstream_plan["excluded_non_arxiv_ids"]})}
    selected = {(observation.SOURCE, "citation_transcript"), (observation.SCOPE_SOURCE, observation.SCOPE_OBJECT)}
    return outputs, selected


def build_documents(plan, sources, manifests, objects, captured, lineages, profile, programs, when):
    io.require(profile in profiles()["profiles"] and profile["files"] ==
        [{"path": path, "sha256": io.sha(raw)} for path, raw in sorted(programs.items())], "unreviewed OpenAlex program preimages")
    reduced = {CHILDREN[0]: reduce_transcript(manifests, objects, captured, lineages, programs)}
    files = {"plan.json": io.canonical(plan), "normalization/profile.json": io.canonical(profile),
        "normalization/configuration.json": io.canonical(CONFIGURATION),
        "normalization/upstream_walk_plan.json": captured[(observation.SOURCE, "normalization_plan")]}
    files.update({"implementation/" + path: raw for path, raw in programs.items()})
    def planned(name, path, roles, media="application/json"):
        raw = files[path]
        item = {"name": name, "root": PHYSICAL_ROOT, "path": path, "sha256": io.sha(raw), "bytes": len(raw),
                "media_type": media, "roles": sorted(roles), "redistribution": "restricted"}
        files.setdefault("objects/sha256/" + item["sha256"], raw)
        return item
    tool = {"name": "wikilean-openalex-normalizer", "version": "1", "sha256": io.sha(io.canonical(profile))}
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
            path = "normalized/" + name + (".jsonl" if name == "arxiv_citations" else ".json")
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
            "license": {"expression": "CC0-1.0",
                "redistribution": "restricted", "notice": "Derived from explicitly reviewed private source evidence; publication is not approved."},
            "acquisition": tool, "normalization": {"schema": schema, "tool": tool,
                "inputs": sorted(item["name"] for item in raw_objects), "outputs": sorted(output)},
            "evidence": {"acquisition_receipts": [], "request_parameter_preimages": [], "normalization_lineage": {
                "root": PHYSICAL_ROOT, "path": path, "sha256": io.sha(files[path]), "bytes": len(files[path]),
                "media_type": "application/json", "normalization_lineage_id": lineage["normalization_lineage_id"]}}}
        manifest = io.source_plan_contracts._source_manifest_from_plan(child, "derived OpenAlex source")
        contracts.validate_source_manifest_evidence_documents(manifest, receipts={}, lineage=lineage,
            request_parameter_preimages={}, parent_source_manifests=parents)
        children.append(child)
        child_manifests.append(manifest)
        files["source-manifests/" + source + ".json"] = io.canonical(manifest)
    fragment = {"schema": "wikilean.openalex-fragment/v1", "scope": "source-plan-fragment", "physical_root": PHYSICAL_ROOT,
        "source_publishable": False, "redistribution": "restricted", "sources": sorted([*sources.values(), *children], key=lambda source: source["source"]),
        "input_bindings": [{"input_id": "external-arxiv-citations", "state": "present", "sources": list(CHILDREN),
            "members": [{"path": "arxiv_citations.jsonl", "source": CHILDREN[0], "object": "arxiv_citations"}]}]}

    files["source-fragment.json"] = io.canonical(fragment)
    document = {"schema": EXPORT_SCHEMA, "normalization_profile_id": profile["profile_id"], "normalized_at": when,
        "source_manifest_ids": sorted(m["source_manifest_id"] for m in child_manifests),
        "files": {path: {"sha256": io.sha(raw), "bytes": len(raw)} for path, raw in sorted(files.items())}}
    document["export_id"] = contracts.domain_hash(EXPORT_SCHEMA, document)
    files["export.json"] = io.canonical(document)
    return files
