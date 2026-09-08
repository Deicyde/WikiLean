"""Evidence-closed, offline normalization for WikiLean's derived catalog inputs.

Parent entries use the existing v3 source-plan representation. The plan's
reviewed_parent_manifest_ids are the explicit trust root: the plan author must
first run each upstream family's specialized export verifier and review those
exact identities. This tool checks their complete byte/evidence closure and
input coherence; it does not independently repeat upstream acquisition. Physical root
paths are supplied separately; they never enter semantic configuration. Selected
objects are copied through retained descriptors into a private workspace before
any reduction. Curated audit trails are read from one exact Git commit.
"""
from __future__ import annotations

import collections
import copy
import csv
import decimal
import hashlib
import json
import os
import re
import stat
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
for directory in (ROOT / "brain/tools", ROOT / "brain/ingest", ROOT / "catalog"):
    sys.path.append(str(directory))
import authority_contracts as contracts
import source_plan_contracts
import git_snapshot
import derived_graph_adapters as adapters
import stage_io

PLAN_SCHEMA = "wikilean.derived-catalog-plan/v1"
PROFILE_SCHEMA = "wikilean.derived-catalog-profiles/v1"
EXPORT_SCHEMA = "wikilean.derived-catalog-export/v1"
NORMALIZATION_SCHEMA = "wikilean.derived-catalog-normalization/v1"
REGISTRY = ROOT / "brain/derived_catalog_profiles.json"
PHYSICAL_ROOT = "derived_catalog"
CURATED_PATHS = {
    "pilot-tagged": "catalog/data/pilot_tagged.jsonl",
    "tier2-tagged": "catalog/data/tier2_tagged.jsonl",
    "grounding": "catalog/data/rebuild_grounding.json",
    "grounding-overrides": "catalog/data/grounding_overrides.jsonl",
}
INPUTS = {"annotations", "declaration-oracle", "mathlib-source-tree", "statement-formal",
          "formal-dependency", "theorem-matching", "wikidata-edges", "wikidata-crossrefs"}
OUTPUTS = {
    "concept-layer": "catalog/data/concept_layer.jsonl",
    "concept-graph": "catalog/data/concept_graph_v2.json",
    "decl-to-qid": "catalog/data/decl_to_qid_v2.json",
    "decl-qid-roles": "catalog/data/decl_qid_roles_v2.json",
    "hierarchy": "catalog/data/hierarchy.json",
    "theoremgraph-links": "catalog/data/theoremgraph_links.json",
}
OPTIONS = {"rebuild_targets": "absent", "theorem_matching_tier": "affirmed",
           "source_rescue": "pinned-mathlib-short-name/v1", "annotation_order": "logical-path-sorted"}
TOOL_FILES = tuple(sorted({
    "brain/derived_catalog_sources.py", "brain/export_derived_catalog.py", "brain/stage_io.py",
    "brain/ingest/git_snapshot.py", "brain/tools/authority_contracts.py",
    "brain/tools/execution_environment.py", "brain/tools/source_plan_contracts.py",
    "catalog/derived_graph_adapters.py", "catalog/build_concept_layer.py", "catalog/build_graph_v2.py",
    "catalog/build_hierarchy.py", "catalog/ingest_theorem_graph.py", "catalog/lift_formal_edges.py",
    "catalog/huggingface_download.py",
}))
MAX_CONTROL = 64 * 1024 * 1024


class DerivationError(RuntimeError):
    pass


def require(condition, message):
    if not condition:
        raise DerivationError(message)


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def canonical(value):
    return contracts.canonical_json_bytes(value)


def artifact(value):
    # The legacy theorem matcher rounds a Python float to three places. Bind
    # that displayed number as a decimal artifact without binary float JSON.
    def numbers(item):
        if isinstance(item, float):
            return decimal.Decimal(str(item))
        if isinstance(item, dict):
            return {key: numbers(child) for key, child in item.items()}
        if isinstance(item, list):
            return [numbers(child) for child in item]
        return item
    return contracts.canonical_artifact_json_bytes(numbers(value))


def exact(value, keys, label):
    require(isinstance(value, dict) and set(value) == set(keys), label + ": unexpected fields")
    return value


def real_path(path):
    require(path.is_absolute() and ".." not in path.parts and not any(p.is_symlink() for p in (path, *path.parents)),
            "input paths must have real absolute ancestry")


def capture_file(path, expected=None, destination=None, *, limit=None, executable=False):
    """One descriptor supplies both the verified digest and copied/read bytes."""
    real_path(path)
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    output = None
    chunks = [] if limit is not None else None
    try:
        before = os.fstat(fd)
        require(stat.S_ISREG(before.st_mode) and (before.st_nlink == 1 or executable), "input must be a regular single-link file")
        if limit is not None:
            require(before.st_size <= limit, "control input exceeds its bound")
        if destination is not None:
            output = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o400)
        digest, count = hashlib.sha256(), 0
        while raw := os.read(fd, 1024 * 1024):
            count += len(raw)
            require(limit is None or count <= limit, "control input grew beyond its bound")
            digest.update(raw)
            if chunks is not None:
                chunks.append(raw)
            if output is not None:
                view = memoryview(raw)
                while view:
                    written = os.write(output, view)
                    require(written > 0, "input copy stopped")
                    view = view[written:]
        signature = lambda metadata: (metadata.st_dev, metadata.st_ino, metadata.st_size, metadata.st_mtime_ns, metadata.st_ctime_ns)
        require(signature(before) == signature(os.fstat(fd)) == signature(path.lstat()), "input changed during capture")
        descriptor = {"sha256": digest.hexdigest(), "bytes": count}
        if expected is not None:
            require(all(descriptor[key] == expected[key] for key in descriptor), "captured input differs from its source object")
        if output is not None:
            os.fsync(output)
        return b"".join(chunks) if chunks is not None else descriptor
    finally:
        os.close(fd)
        if output is not None:
            os.close(output)


def read(path, expected=None):
    return capture_file(path, expected, limit=MAX_CONTROL)


def profile_id(profile):
    return contracts.domain_hash("wikilean.derived-catalog-profile.v1", {"files": profile["files"]})


def profiles():
    raw = read(REGISTRY)
    registry = exact(contracts.parse_json_bytes(raw, location="profiles"), {"schema", "current_profile", "profiles"}, "profiles")
    require(raw == canonical(registry) and registry["schema"] == PROFILE_SCHEMA, "invalid profile registry")
    ids = []
    for profile in registry["profiles"]:
        exact(profile, {"profile_id", "files"}, "profile")
        require([item["path"] for item in profile["files"]] == list(TOOL_FILES), "profile must close every executed helper")
        for item in profile["files"]:
            exact(item, {"path", "sha256"}, "helper")
            contracts._digest(item["sha256"], "helper digest")
        require(profile["profile_id"] == profile_id(profile), "profile identity differs")
        ids.append(profile["profile_id"])
    require(ids == sorted(set(ids)) and registry["current_profile"] in ids, "invalid current profile")
    return registry


def module_origins():
    pairs = [(contracts, "brain/tools/authority_contracts.py"),
             (contracts.execution_environment_contract, "brain/tools/execution_environment.py"),
             (source_plan_contracts, "brain/tools/source_plan_contracts.py"),
             (git_snapshot, "brain/ingest/git_snapshot.py"), (stage_io, "brain/stage_io.py"),
             (adapters, "catalog/derived_graph_adapters.py")]
    for name in ("build_concept_layer", "build_graph_v2", "build_hierarchy", "ingest_theorem_graph", "lift_formal_edges"):
        pairs.append((getattr(adapters, name), "catalog/" + name + ".py"))
    pairs.append((sys.modules["huggingface_download"], "catalog/huggingface_download.py"))
    for module, relative in pairs:
        require(Path(module.__file__).resolve() == ROOT / relative, "loaded helper origin differs: " + relative)
    require(source_plan_contracts.contracts is contracts, "source helper loaded another contract module")


def current_profile():
    module_origins()
    registry = profiles()
    profile = next(profile for profile in registry["profiles"] if profile["profile_id"] == registry["current_profile"])
    actual = [{"path": relative, "sha256": capture_file(ROOT / relative)["sha256"]} for relative in TOOL_FILES]
    require(profile["files"] == actual, "current normalizer differs from the reviewed whole generation")
    return copy.deepcopy(profile)


def physical(ref, roots):
    contracts.validate_literal_relative_path(ref["path"], "source object path")
    require(ref["root"] in roots, "missing explicit physical source root")
    path = roots[ref["root"]] / ref["path"]
    real_path(path)
    return path


def validate_plan(plan):
    exact(plan, {"schema", "curated_git_commit", "parents", "reviewed_parent_manifest_ids", "bindings", "options"}, "derived plan")
    require(plan["schema"] == PLAN_SCHEMA and plan["options"] == OPTIONS, "unsupported normalization plan/options")
    contracts._expect_pattern(plan["curated_git_commit"], "curated commit", contracts.GIT_COMMIT_RE, "a full commit")
    require(isinstance(plan["parents"], list) and plan["parents"], "explicit parent sources are required")
    parents = {source["source"]: source for source in plan["parents"]}
    require(len(parents) == len(plan["parents"]), "duplicate parent source")
    require(isinstance(plan["reviewed_parent_manifest_ids"], dict) and set(plan["reviewed_parent_manifest_ids"]) == set(parents),
            "every parent needs an explicitly reviewed source manifest identity")
    for identity in plan["reviewed_parent_manifest_ids"].values():
        contracts._hash(identity, "reviewed parent source manifest")
    require(set(plan["bindings"]) == INPUTS, "exact derived input closure is required")
    for name, members in plan["bindings"].items():
        require(isinstance(members, list) and members, "explicit nonempty input members are required: " + name)
        if name not in {"annotations", "mathlib-source-tree"}:
            require(len(members) == 1, "singleton input has extra members")
        paths = []
        for member in members:
            exact(member, {"path", "source", "object"}, "input member")
            contracts.validate_literal_relative_path(member["path"], "input logical path")
            require(member["source"] in parents, "input names an undeclared source")
            paths.append(member["path"])
        require(paths == sorted(set(paths)), "input members must have unique sorted logical paths")
    return plan


def capture_parents(plan, roots, workspace):
    """Verify all parent evidence and copy the exact selected generation once."""
    validate_plan(plan)
    sources = {item["source"]: copy.deepcopy(item) for item in plan["parents"]}
    manifests = {name: source_plan_contracts._source_manifest_from_plan(source, "parent " + name)
                 for name, source in sources.items()}
    require({name: manifest["source_manifest_id"] for name, manifest in manifests.items()} == plan["reviewed_parent_manifest_ids"],
            "parent source differs from its explicitly reviewed manifest identity")
    by_id = {manifest["source_manifest_id"]: manifest for manifest in manifests.values()}
    require(len(by_id) == len(manifests), "duplicate parent identity")
    objects = {(name, item["name"]): item for name, source in sources.items() for item in source["objects"]}
    selected = {(member["source"], member["object"]) for members in plan["bindings"].values() for member in members}
    mathlib_sources = {member["source"] for member in plan["bindings"]["mathlib-source-tree"]}
    require(len(mathlib_sources) == 1, "Mathlib source files require one exact source generation")
    selected.add((next(iter(mathlib_sources)), "git_tree"))
    selected.add((selected_member(plan, "wikidata-crossrefs")["source"], "requested_qid_scope"))
    observation_plan_key = (selected_member(plan, "wikidata-edges")["source"], "request_plan")
    selected.add(observation_plan_key)
    require(selected <= set(objects), "input object absent from its parent")
    captured = {}
    for key, item in objects.items():
        destination = workspace / item["sha256"] if key in selected else None
        if destination is not None and destination.exists():
            capture_file(physical(item, roots), item)
            capture_file(destination, item)
        else:
            capture_file(physical(item, roots), item, destination)
        if destination is not None:
            require("normalized" in item["roles"] or (key == observation_plan_key and "receipt" in item["roles"]),
                    "derived inputs require explicitly normalized source objects or the retained observation plan")
            captured[key] = destination
    evidence_bytes, lineages = {}, {}
    for name, source in sources.items():
        if source["source_kind"] == "curated_git_tree":
            continue
        evidence = source["evidence"]
        receipts = {}
        for ref in evidence["acquisition_receipts"]:
            raw = read(physical(ref, roots), ref)
            document = contracts.parse_json_bytes(raw, location="parent receipt")
            receipts[ref["acquisition_receipt_id"]] = document
            evidence_bytes[(ref["root"], ref["path"])] = raw
        ref = evidence["normalization_lineage"]
        raw = read(physical(ref, roots), ref)
        lineage = contracts.parse_json_bytes(raw, location="parent lineage")
        lineages[name] = lineage
        evidence_bytes[(ref["root"], ref["path"])] = raw
        preimages = {}
        for ref in evidence["request_parameter_preimages"]:
            raw = read(physical(ref, roots), ref)
            evidence_bytes[(ref["root"], ref["path"])] = raw
            preimages[ref["parameters_sha256"]] = {key: ref[key] for key in ("parameters_sha256", "bytes", "media_type")}
        require(set(lineage["parent_source_manifest_ids"]) <= set(by_id), "normalization parent evidence is missing")
        contracts.validate_source_manifest_evidence_documents(manifests[name], receipts=receipts, lineage=lineage,
            request_parameter_preimages=preimages,
            parent_source_manifests={key: by_id[key] for key in lineage["parent_source_manifest_ids"]})
    return sources, manifests, objects, captured, evidence_bytes, lineages


def json_object(path):
    raw = capture_file(path, limit=512 * 1024 * 1024)
    return contracts.parse_artifact_json_bytes(raw, location="captured source object")


def json_lines(path):
    with path.open("rb") as handle:
        for raw in handle:
            if raw.strip():
                value = contracts.parse_artifact_json_bytes(raw, location="captured JSONL row")
                require(isinstance(value, dict), "source JSONL rows must be objects")
                yield value


def csv_rows(path):
    with path.open(encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        require(reader.fieldnames is not None and len(reader.fieldnames) == len(set(reader.fieldnames)), "CSV headers must be present and unique")
        for row in reader:
            require(None not in row and all(value is not None for value in row.values()), "CSV rows must have exactly their declared columns")
            yield row


def capture_curated(repository, commit, *, git="/usr/bin/git"):
    """Read four actual Git blobs; neither index nor worktree supplies input."""
    command = git_snapshot._validated_git(git)
    repository = git_snapshot._validated_repository(repository)
    git_snapshot._require_top_level(command, repository)
    git_snapshot._reject_partial_clone(command, repository)
    contracts._expect_pattern(commit, "curated commit", contracts.GIT_COMMIT_RE, "full commit")
    object_type = git_snapshot._run_git(command, repository, ["cat-file", "-t", commit])
    require(object_type == b"commit\n", "curated pin must name a commit object")
    tree = git_snapshot._commit_tree(command, repository, commit)
    entries = []
    for name, path in sorted(CURATED_PATHS.items()):
        selected = git_snapshot._select_entries(git_snapshot._tree_entries(command, repository, commit, path), path, None)
        entries.extend(selected)
    captured = {item.path: item.text.encode("utf-8") for item in git_snapshot._read_text_blobs(command, repository, tuple(entries))}
    proof = {"commit": git_snapshot._run_git(command, repository, ["cat-file", "commit", commit])}
    oid = tree
    for component in (None, "catalog", "data"):
        if component is not None:
            mode, oid = parse_git_tree(proof[oid])[component]
            require(mode == "40000", "curated ancestor is not a Git tree")
        proof[oid] = git_snapshot._run_git(command, repository, ["cat-file", "tree", oid])
    version = git_snapshot._run_git(command, repository, ["--version"]).decode("ascii").strip()
    tool = {"schema": "wikilean.curated-git-reader/v1", "git_sha256": capture_file(Path(command), executable=True)["sha256"], "git_version": version}
    curated = {name: captured[path] for name, path in CURATED_PATHS.items()}
    verify_curated_proof(curated, commit, tree, proof)
    return curated, tree, tool, proof


def git_oid(kind, raw):
    return hashlib.sha1(kind.encode() + b" " + str(len(raw)).encode() + b"\0" + raw).hexdigest()


def parse_git_tree(raw):
    entries = {}
    while raw:
        header, separator, remainder = raw.partition(b"\0")
        require(bool(separator) and len(remainder) >= 20, "truncated Git tree proof")
        mode, separator, name = header.partition(b" ")
        require(bool(separator), "invalid Git tree header")
        name, mode = name.decode("utf-8"), mode.decode("ascii")
        require(name not in entries and name not in {"", ".", ".."} and "/" not in name, "invalid Git tree path")
        entries[name] = (mode, remainder[:20].hex())
        raw = remainder[20:]
    return entries


def verify_curated_proof(curated, commit, tree, proof):
    require(set(curated) == set(CURATED_PATHS), "curated blob closure differs")
    require(git_oid("commit", proof["commit"]) == commit and proof["commit"].startswith(("tree " + tree + "\n").encode()), "Git commit/tree proof differs")
    used = {"commit"}
    for name, path in CURATED_PATHS.items():
        oid = tree
        parts = path.split("/")
        for index, component in enumerate(parts):
            require(oid in proof and git_oid("tree", proof[oid]) == oid, "Git tree object proof differs")
            used.add(oid)
            entries = parse_git_tree(proof[oid])
            require(component in entries, "curated blob is absent from the pinned Git tree")
            mode, child = entries[component]
            if index == len(parts) - 1:
                require(mode in {"100644", "100755"} and git_oid("blob", curated[name]) == child, "curated blob content is not its pinned regular Git object")
            else:
                require(mode == "40000", "curated Git ancestor is not a tree")
                oid = child
    require(set(proof) == used, "curated proof contains undeclared objects")


def selected_member(plan, name):
    return plan["bindings"][name][0]


def member_path(member, captured):
    return captured[(member["source"], member["object"])]


def input_contracts(plan, manifests, objects, captured, lineages):
    """Enforce complete D1/Mathlib selection and same-generation HF dependencies."""
    def source(name):
        return manifests[selected_member(plan, name)["source"]]
    statement = selected_member(plan, "statement-formal")
    dependency = selected_member(plan, "formal-dependency")
    require(statement["source"] == dependency["source"] and
            (statement["object"], dependency["object"]) == ("statement_formal_csv", "formal_dependency_csv"),
            "formal statements and dependencies require the same complete HF generation")
    require(source("statement-formal")["source"] == "hf-uw-math-graph" and source("statement-formal")["source_kind"] == "acquired_dataset",
            "formal statements require the verified math-graph source")
    require(source("theorem-matching")["source"] == "hf-uw-theorem-matching" and
            selected_member(plan, "theorem-matching")["object"] == "theorem_matching_csv",
            "theorem matches require the verified theorem-matching source")
    for name in ("statement-formal", "theorem-matching"):
        require(source(name)["pin"]["type"] == "git_commit", "HF inputs require full immutable commit pins")
    annotations = plan["bindings"]["annotations"]
    annotation_sources = {member["source"] for member in annotations}
    require(len(annotation_sources) == 1, "annotations require one complete D1-derived generation")
    annotation_source = next(iter(annotation_sources))
    require(manifests[annotation_source]["source_kind"] == "sealed_snapshot", "annotations must derive from captured D1 evidence")
    expected_articles = {name for (origin, name), item in objects.items() if origin == annotation_source and name.startswith("article-") and "normalized" in item["roles"]}
    require({member["object"] for member in annotations} == expected_articles, "annotation selection omits or substitutes captured articles")
    for member in annotations:
        article = json_object(member_path(member, captured))
        import unicodedata
        require(member["path"] == "site/annotations/" + unicodedata.normalize("NFC", article["slug"]) + ".json",
                "annotation logical path differs from its captured slug")
    mathlib = source("mathlib-source-tree")
    mathlib_name = mathlib["source"]
    require(mathlib["source_kind"] == "acquired_dataset" and mathlib["pin"]["type"] == "git_commit", "Mathlib requires the official acquired source commit")
    tree = json_object(captured[(mathlib_name, "git_tree")])
    require(tree["commit"] == mathlib["pin"]["value"], "Mathlib tree index and source commit disagree")
    expected = {entry["path"]: entry for entry in tree["entries"] if entry["path"].startswith("Mathlib/") and entry["path"].endswith(".lean")}
    require({member["path"] for member in plan["bindings"]["mathlib-source-tree"]} == set(expected), "Mathlib source selection is incomplete")
    for member in plan["bindings"]["mathlib-source-tree"]:
        entry = expected[member["path"]]
        require(entry["mode"] in {"100644", "100755"} and member["object"] == "file-" + sha(member["path"].encode()), "Mathlib member is not its exact regular Git path")
        item = objects[(member["source"], member["object"])]
        require(all(item[key] == entry[key] for key in ("sha256", "bytes")), "Mathlib tree/member content differs")
    oracle = source("declaration-oracle")
    require(oracle["source"] == "mathlib-docs" and selected_member(plan, "declaration-oracle")["object"] == "declaration_oracle",
            "oracle requires the independently verified official docs source")
    require(mathlib["source_manifest_id"] in lineages[oracle["source"]]["parent_source_manifest_ids"], "oracle does not descend from the selected Mathlib source")
    require(source("wikidata-edges")["source_kind"] == "acquired_dataset" and
            selected_member(plan, "wikidata-edges")["object"] == "wikidata_edges", "edges require the complete acquired Wikidata observation")
    require(source("wikidata-crossrefs")["source_kind"] in {"acquired_dataset", "sealed_snapshot"}, "crossrefs require acquired Wikidata lineage")
    crossrefs = json_object(member_path(selected_member(plan, "wikidata-crossrefs"), captured))
    require(isinstance(crossrefs, dict) and isinstance(crossrefs.get("xrefs"), dict), "crossref normalization must provide a QID-indexed xrefs object")
    require(source("wikidata-crossrefs")["source"] == "wikidata-crossrefs" and
            selected_member(plan, "wikidata-crossrefs")["object"] == "wikidata_crossrefs", "crossrefs require the reviewed entity-derived normalization")
    scope = json_object(captured[(source("wikidata-crossrefs")["source"], "requested_qid_scope")])
    exact(scope, {"schema", "qids"}, "crossref scope")
    require(scope["schema"] == "wikilean.wikidata-crossref-scope/v1" and isinstance(scope["qids"], list) and
            all(isinstance(qid, str) and re.fullmatch(r"Q[1-9][0-9]*", qid) for qid in scope["qids"]) and
            scope["qids"] == sorted(set(scope["qids"])) and set(crossrefs["xrefs"]) <= set(scope["qids"]),
            "crossref queried scope is invalid or excludes a returned QID")


def hf_metadata(plan, name, manifests, objects):
    member = selected_member(plan, name)
    source = manifests[member["source"]]
    dataset = "uw-math-ai/math-graph" if name == "statement-formal" else "uw-math-ai/theorem-matching"
    filename = "statement_formal.csv" if name == "statement-formal" else "theorem_matching.csv"
    item = objects[(member["source"], member["object"])]
    return {"revision": source["pin"]["value"], "file_url": "https://huggingface.co/datasets/" + dataset + "/resolve/" + source["pin"]["value"] + "/" + filename,
            "sha256": item["sha256"], "size": item["bytes"]}


def reduce_inputs(plan, curated, manifests, objects, captured, lineages):
    input_contracts(plan, manifests, objects, captured, lineages)
    def document(name):
        return json_object(member_path(selected_member(plan, name), captured))
    def rows(name):
        return csv_rows(member_path(selected_member(plan, name), captured))
    def curated_json(name):
        return contracts.parse_artifact_json_bytes(curated[name], location="curated Git blob")
    def curated_rows(name):
        return [contracts.parse_artifact_json_bytes(raw, location="curated Git row") for raw in curated[name].splitlines() if raw.strip()]
    layer = adapters.concept_layer({"pilot_tagged.jsonl": curated_rows("pilot-tagged"), "tier2_tagged.jsonl": curated_rows("tier2-tagged")})
    prior = adapters.prior_nodes(layer)
    annotations = [(member["path"], json_object(member_path(member, captured))) for member in plan["bindings"]["annotations"]]
    grounding = curated_json("grounding")
    if isinstance(grounding, dict):
        grounding = grounding.get("concepts", [])
    require(isinstance(grounding, list) and grounding, "grounding audit trail must be nonempty")
    crossref_scope = json_object(captured[(selected_member(plan, "wikidata-crossrefs")["source"], "requested_qid_scope")])
    require({row["qid"] for row in grounding} <= set(crossref_scope["qids"]), "crossref queried scope omits a grounded QID")
    observation_plan = json_object(captured[(selected_member(plan, "wikidata-edges")["source"], "request_plan")])
    require(observation_plan.get("schema") in {"wikilean.wikidata-observation-plan/v1", "wikilean.wikidata-observation-plan/v2"} and
            isinstance(observation_plan.get("edge_qids"), list) and
            all(isinstance(qid, str) and re.fullmatch(r"Q[1-9][0-9]*", qid) for qid in observation_plan["edge_qids"]) and
            observation_plan["edge_qids"] == sorted(set(observation_plan["edge_qids"]), key=lambda qid: (len(qid), qid)) and
            {row["qid"] for row in grounding} <= set(observation_plan["edge_qids"]),
            "Wikidata edge observation scope omits a grounded QID or has an unsupported plan")
    source_text = (capture_file(member_path(member, captured), limit=64 * 1024 * 1024).decode("utf-8")
                   for member in plan["bindings"]["mathlib-source-tree"])
    graph = adapters.concept_graph(grounding=grounding, overrides=curated_rows("grounding-overrides"),
        crossrefs=document("wikidata-crossrefs"), prior=prior, annotations=annotations,
        oracle=document("declaration-oracle"), mathlib_text=source_text,
        statements=rows("statement-formal"), dependencies=rows("formal-dependency"),
        wikidata_edges=json_lines(member_path(selected_member(plan, "wikidata-edges"), captured)))
    return {"concept-layer": b"".join(artifact(row) + b"\n" for row in layer),
            **{name: artifact(value) for name, value in graph.items()},
            "hierarchy": artifact(adapters.hierarchy(rows("statement-formal"), hf_metadata(plan, "statement-formal", manifests, objects))),
            "theoremgraph-links": artifact(adapters.theoremgraph_links(prior=prior, annotations=annotations,
                matches=rows("theorem-matching"), metadata=hf_metadata(plan, "theorem-matching", manifests, objects)))}


def object_ref(item):
    return {"object": item["name"], **{key: item[key] for key in ("sha256", "bytes", "media_type")}}


def file_ref(path, raw, media_type="application/json"):
    return {"root": PHYSICAL_ROOT, "path": path, "sha256": sha(raw), "bytes": len(raw), "media_type": media_type}


def build_documents(plan, curated, tree, git_tool, proof, sources, manifests, objects,
                    output, profile, implementation, normalized_at):
    """Construct a source closure; external parent roots keep their exact evidence."""
    require(set(output) == set(OUTPUTS), "normalization output closure differs")
    require(set(implementation) == set(TOOL_FILES) and
            profile["files"] == [{"path": path, "sha256": sha(implementation[path])} for path in TOOL_FILES],
            "normalizer bytes do not match their whole profile preimage")
    require(profile in profiles()["profiles"], "unreviewed normalization profile")
    files = {"plan.json": canonical(plan), "normalization/tool-profile.json": canonical(profile),
             "curated/git-tool.json": canonical(git_tool)}
    for path, raw in implementation.items():
        files["implementation/" + path] = raw
    for name, raw in curated.items():
        files["curated/" + name] = raw
    for name, raw in proof.items():
        files["curated/proof/" + name] = raw
    tool = {"name": "wikilean-derived-catalog", "version": "1", "sha256": sha(canonical(profile))}
    def planned(name, path, roles, media_type="application/json"):
        raw = files[path]
        return {"name": name, **file_ref(path, raw, media_type), "roles": sorted(roles), "redistribution": "restricted"}
    curation_name = "wikilean-derived-curation"
    curated_objects = [planned(name, "curated/" + name, ["raw", "normalized"],
                              "application/x-ndjson" if name != "grounding" else "application/json")
                       for name in sorted(curated)]
    # The pack compiler reads curated objects from this actual pinned Git
    # tree. Private proof copies are support objects, never Git members.
    curated_objects = [{**item, "root": "repo", "path": CURATED_PATHS[item["name"]]} for item in curated_objects]
    curation = {"source": curation_name, "source_kind": "curated_git_tree",
        "pin": {"type": "git_commit", "value": plan["curated_git_commit"], "tree": tree},
        "objects": curated_objects,
        "license": {"expression": "LicenseRef-WikiLean-Curated-Audit", "redistribution": "restricted",
                    "notice": "Committed annotation and grounding audit trails; private migration input."},
        "acquisition": {"name": "wikilean-curated-git-reader", "version": "1", "sha256": sha(canonical(git_tool))},
        "normalization": {"schema": "wikilean.curated-git-identity/v1", "tool": tool,
                          "inputs": sorted(curated), "outputs": sorted(curated)}}
    sources = copy.deepcopy(sources)
    manifests = copy.deepcopy(manifests)
    require(curation_name not in sources, "parent source collides with derived curation")
    sources[curation_name] = curation
    manifests[curation_name] = source_plan_contracts._source_manifest_from_plan(curation, "curated source")
    objects = {**objects, **{(curation_name, item["name"]): item for item in curation["objects"]}}
    config = {"schema": NORMALIZATION_SCHEMA, "options": plan["options"], "curated_git_commit": plan["curated_git_commit"],
              "parents": sorted(manifest["source_manifest_id"] for manifest in manifests.values()),
              "bindings": {name: [{"path": member["path"], "source_manifest_id": manifests[member["source"]]["source_manifest_id"],
                                   **object_ref(objects[(member["source"], member["object"])])}
                                  for member in members] for name, members in plan["bindings"].items()}}
    files["normalization/configuration.json"] = canonical(config)
    support = [planned("normalizer-profile", "normalization/tool-profile.json", ["receipt"]),
               planned("normalizer-configuration", "normalization/configuration.json", ["receipt"]),
               planned("git-reader", "curated/git-tool.json", ["receipt"]),
               *(planned("git-proof-" + name, "curated/proof/" + name, ["receipt"], "application/octet-stream") for name in sorted(proof)),
               *(planned("normalizer-program-" + str(index), "implementation/" + path, ["receipt"], "text/x-python")
                 for index, path in enumerate(TOOL_FILES))]
    observation_plan_object = objects[(selected_member(plan, "wikidata-edges")["source"], "request_plan")]
    families = [
        ("concept-layer", ["concept-layer"], ["pilot-tagged", "tier2-tagged"], []),
        ("concept-graph", ["concept-graph", "decl-to-qid", "decl-qid-roles"], ["grounding", "grounding-overrides"],
         ["annotations", "declaration-oracle", "mathlib-source-tree", "statement-formal", "formal-dependency", "wikidata-edges", "wikidata-crossrefs"]),
        ("hierarchy", ["hierarchy"], [], ["statement-formal"]),
        ("theoremgraph-links", ["theoremgraph-links"], [], ["annotations", "theorem-matching"]),
    ]
    bindings = []
    generated = []
    for family, outputs, curated_inputs, parent_inputs in families:
        name = "wikilean-derived-" + family
        require(name not in sources, "parent source collides with derived family")
        selected = {(curation_name, item) for item in curated_inputs}
        selected.update((member["source"], member["object"]) for input_name in parent_inputs for member in plan["bindings"][input_name])
        if family == "concept-graph":
            selected.add((selected_member(plan, "mathlib-source-tree")["source"], "git_tree"))
            selected.add((selected_member(plan, "wikidata-crossrefs")["source"], "requested_qid_scope"))
        if family in {"concept-graph", "theoremgraph-links"}:
            selected.add(("wikilean-derived-concept-layer", "concept-layer"))
        raw_objects = []
        lineage_inputs = []
        for source_name, object_name in sorted(selected, key=lambda key: (key[1], key[0])):
            item = objects[(source_name, object_name)]
            require("normalized" in item["roles"], "derived lineage input is not normalized")
            raw_objects.append(planned(object_name, "curated/" + object_name, ["raw"], item["media_type"])
                               if source_name == curation_name else {**item, "roles": ["raw"]})
            lineage_inputs.append({**object_ref(item), "origin": {"kind": "source_manifest", "id": manifests[source_name]["source_manifest_id"]}})
        require(len({item["name"] for item in raw_objects}) == len(raw_objects), "parent object names collide within a derived family")
        lineage_inputs.sort(key=lambda item: (item["origin"]["kind"], item["origin"]["id"], item["object"]))
        normalized = []
        for output_name in outputs:
            path = "normalized/" + OUTPUTS[output_name]
            files[path] = output[output_name]
            item = planned(output_name, path, ["normalized"], "application/x-ndjson" if output_name == "concept-layer" else "application/json")
            normalized.append(item)
            bindings.append({"input_id": output_name, "sources": [name], "state": "present",
                "members": [{"path": OUTPUTS[output_name], "source": name, "object": output_name}]})
        parents = {manifests[source_name]["source_manifest_id"]: manifests[source_name] for source_name, _ in selected}
        lineage = {"schema": contracts.NORMALIZATION_LINEAGE_SCHEMA_V1,
            "normalization_lineage_id": "sha256:" + "0" * 64, "source": name, "mode": "transform",
            "acquisition_receipt_ids": [], "parent_source_manifest_ids": sorted(parents),
            "normalization_schema": NORMALIZATION_SCHEMA, "configuration_sha256": sha(files["normalization/configuration.json"]),
            "tool": tool, "inputs": lineage_inputs, "outputs": [object_ref(item) for item in sorted(normalized, key=lambda item: item["name"])],
            "result": "complete", "audit": {"normalized_at": normalized_at}}
        lineage["normalization_lineage_id"] = contracts.normalization_lineage_identity(lineage)
        lineage_path = "evidence/" + family + "-lineage.json"
        files[lineage_path] = canonical(lineage)
        source = {"source": name, "source_kind": "sealed_snapshot",
            "pin": {"type": "dataset_revision", "value": "sha256:" + sha(canonical({"family": family, "inputs": lineage_inputs, "configuration": sha(files["normalization/configuration.json"])}))},
            "objects": sorted([*raw_objects, *normalized, *support,
                *([{**observation_plan_object, "name": "parent-wikidata-request-plan", "roles": ["receipt"]}] if family == "concept-graph" else [])],
                key=lambda item: item["name"]),
            "license": {"expression": "LicenseRef-WikiLean-Derived-Review", "redistribution": "restricted",
                        "notice": "Private derived migration input. Original parent licenses and acquisition evidence remain authoritative."},
            "acquisition": tool,
            "normalization": {"schema": NORMALIZATION_SCHEMA, "tool": tool,
                              "inputs": sorted(item["name"] for item in raw_objects), "outputs": sorted(outputs)},
            "evidence": {"acquisition_receipts": [], "request_parameter_preimages": [],
                         "normalization_lineage": {**file_ref(lineage_path, files[lineage_path]), "normalization_lineage_id": lineage["normalization_lineage_id"]}}}
        manifest = source_plan_contracts._source_manifest_from_plan(source, "derived family")
        contracts.validate_source_manifest_evidence_documents(manifest, receipts={}, lineage=lineage,
            request_parameter_preimages={}, parent_source_manifests=parents)
        sources[name], manifests[name] = source, manifest
        objects.update({(name, item["name"]): item for item in source["objects"]})
        generated.append(name)
    for name, manifest in manifests.items():
        files["source-manifests/" + name + ".json"] = canonical(manifest)
    files["source-fragment.json"] = canonical({"schema": "wikilean.derived-catalog-fragment/v1", "scope": "source-plan-fragment",
        "physical_root": PHYSICAL_ROOT, "source_publishable": False, "redistribution": "restricted",
        "sources": [sources[name] for name in sorted(sources)], "input_bindings": sorted(bindings, key=lambda item: item["input_id"])})
    export = {"schema": EXPORT_SCHEMA, "normalization_profile_id": profile["profile_id"],
              "normalized_at": normalized_at, "curated_tree": tree,
              "derived_source_manifest_ids": {name: manifests[name]["source_manifest_id"] for name in generated},
              "files": [{key: value for key, value in file_ref(path, files[path], "application/octet-stream").items() if key != "root"}
                        for path in sorted(files)]}
    export["export_id"] = contracts.domain_hash("wikilean.derived-catalog-export.v1", export)
    files["export.json"] = canonical(export)
    return files
