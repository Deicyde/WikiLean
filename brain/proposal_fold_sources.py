"""Private proposal replay with explicit Git curation and reviewed parent IDs.

Plan authors independently verify the upstream source exports before reviewing
their exact manifest IDs. This consumer checks every retained object/evidence
member, ancestry and selected family. It does not invent upstream acquisition for
Git curation. The export is a candidate source closure and never installs or binds
the resulting graph. The comparison source is deliberately separate from the
proposal contribution source.
"""
from __future__ import annotations

import copy
import hashlib
import os
import stat
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.append(str(ROOT / "brain"))
import wikidata_crossref_sources as io
import proposal_fold_adapter as adapter

contracts = io.contracts
PLAN_SCHEMA = "wikilean.proposal-fold-source-plan/v1"
CURRENT_GENERATION = 2
EXPORT_SCHEMAS = {
    1: "wikilean.proposal-fold-source-export/v1",
    2: "wikilean.proposal-fold-source-export/v2",
}
EXPORT_SCHEMA = EXPORT_SCHEMAS[CURRENT_GENERATION]
PROFILE_SCHEMA = "wikilean.proposal-fold-source-profiles/v2"
REGISTRY = ROOT / "brain/proposal_fold_profiles.json"
PHYSICAL_ROOT = "proposal_fold_export"
GIT_ROOT = "proposal_git"
PROPOSALS = "wikilean-proposal-curation"
MANUAL_BEFORE = "wikilean-manual-container-predecessor"
MANUAL_AFTER = "wikilean-manual-container-curation"
BASELINE = "wikilean-fold-comparison-baseline"
FOLD = "wikilean-proposal-fold"
MANUAL = "wikilean-manual-container-contributions"
COMPOSED = "wikilean-folded-container-projection"
COMPARISON = "wikilean-proposal-fold-comparison"
CHILDREN = (FOLD, MANUAL, COMPOSED, COMPARISON)
CONTAINER_PATH = "brain/data/container_links.jsonl"
MANUAL_KEYS = frozenset({("Q11348", "Mathlib/Logic/Function"), ("Q11205", "Mathlib/NumberTheory"),
    ("Q11563", "Mathlib/Data/Nat"), ("Q395", "Mathlib"), ("Q903783", "Mathlib/SetTheory")})
BASE_PATHS = {"grounding-overrides-baseline": "catalog/data/grounding_overrides.jsonl",
    "universe-extension-baseline": "catalog/data/universe_extension.jsonl"}
COMPARE_PATHS = {"prior-containers": CONTAINER_PATH, "prior-discovery": "brain/data/discovery_proposals.jsonl",
    "prior-fc": "brain/data/fc_links.jsonl"}
BINDINGS = {
    "grounding": (None, "catalog/data/rebuild_grounding.json", "catalog/data/rebuild_grounding.json"),
    "registry": (None, "catalog/data/source_registry.json", "catalog/data/source_registry.json"),
    "hierarchy": ("wikilean-derived-hierarchy", "hierarchy", "catalog/data/hierarchy.json"),
    "universe": ("wikidata-observation", "wikidata_universe", "catalog/data/wikidata_universe.jsonl"),
    "formal-conjectures": ("git-harvest-formal-conjectures", "formal-conjectures", "catalog/data/formal_conjectures.jsonl"),
    "oracle": ("mathlib-docs", "declaration_oracle", "oracle.json"),
    "mathlib": ("mathlib-source", "git_tree", None),
}
CONFIGURATION_V1 = {"schema": "wikilean.proposal-fold-normalization/v1",
    "fold": "exact reviewed main and helper AST over captured in-memory inputs",
    "mathlib_fallback": "legacy line-pattern search over complete pinned Mathlib subtree, unique .lean module resolution",
    "initial_fc_seed": "absent; separately compare against the pinned prior seed",
    "entity_acquisition": "v1 requires a recomputed empty request plan",
    "xref_and_repo_scope": "v1 rejects all such proposals and prior seeds",
    "manual_curation": "exact five appended native Git contributions, independent of proposal decisions",
    "projection": "retain independent contribution records; equal edge rows may share one projection; conflicting rows reject",
    "authority": "candidate sources only; graph delta and baseline require separate review"}
CONFIGURATION_V2 = {**CONFIGURATION_V1,
    "schema": "wikilean.proposal-fold-normalization/v2",
    "empty_object_media_type": "application/octet-stream iff actual bytes are empty; otherwise retain the declared semantic type"}
TOOL_FILES = tuple(sorted((set(io.TOOL_FILES) - {"brain/export_wikidata_crossrefs.py"}) | {
    "brain/proposal_fold_sources.py", "brain/export_proposal_fold.py", "brain/proposal_fold_adapter.py", "brain/fold_proposals.py"}))


def origins():
    io.origins()
    io.require(Path(adapter.__file__).resolve() == ROOT / "brain/proposal_fold_adapter.py", "fold adapter origin differs")


origins()
LOADED = {path: io.sha(io.read(ROOT / path)) for path in TOOL_FILES if path != "brain/export_proposal_fold.py"}


def profile_id(profile):
    generation = profile_generation(profile)
    payload = {"files": profile["files"]}
    if generation == 2:
        payload["generation"] = generation
    return contracts.domain_hash(f"wikilean.proposal-fold-source-profile.v{generation}", payload)


def profile_generation(profile):
    """Keep the original files-only profile as generation 1 forever."""
    io.require(isinstance(profile, dict), "invalid fold profile generation")
    keys = set(profile) - {"profile_id"}
    if keys == {"files"}:
        return 1
    io.require(keys == {"generation", "files"} and profile["generation"] == 2,
               "invalid fold profile generation")
    return 2


def profiles():
    raw = io.read(REGISTRY)
    value = io.exact(io.parse(raw, "profiles"), {"schema", "current_profile", "profiles"}, "profiles")
    io.require(raw == io.canonical(value) and value["schema"] == PROFILE_SCHEMA and isinstance(value["profiles"], list), "invalid fold profile registry")
    ids = []
    for profile in value["profiles"]:
        generation = profile_generation(profile)
        io.exact(profile, {"profile_id", "files"} | ({"generation"} if generation == 2 else set()), "profile")
        io.require(isinstance(profile["files"], list) and [item["path"] for item in profile["files"]] == list(TOOL_FILES), "fold profile omits its exact executed closure")
        for item in profile["files"]:
            io.exact(item, {"path", "sha256"}, "helper"); contracts._digest(item["sha256"], "helper hash")
        io.require(profile_id(profile) == profile["profile_id"], "fold profile identity differs")
        ids.append(profile["profile_id"])
    io.require(ids == sorted(set(ids)) and value["current_profile"] in ids, "invalid current fold profile")
    return value


def current_profile():
    origins()
    registry = profiles()
    profile = next(p for p in registry["profiles"] if p["profile_id"] == registry["current_profile"])
    io.require(profile_generation(profile) == CURRENT_GENERATION,
               "current fold profile is not the current exporter generation")
    actual = {path: io.sha(io.read(ROOT / path)) for path in TOOL_FILES}
    io.require(profile["files"] == [{"path": path, "sha256": actual[path]} for path in TOOL_FILES], "unreviewed fold implementation")
    io.require(all(actual[path] == digest for path, digest in LOADED.items()), "loaded fold implementation changed")
    return profile


def validate_plan(plan):
    io.exact(plan, {"schema", "parents", "reviewed_parent_manifest_ids", "bindings", "proposal_git_commit", "manual_git_commit"}, "fold plan")
    io.require(plan["schema"] == PLAN_SCHEMA and isinstance(plan["parents"], list) and 1 <= len(plan["parents"]) <= 100, "invalid fold parent plan")
    names = [source["source"] for source in plan["parents"]]
    io.require(names == sorted(set(names)) and not set(names) & ({PROPOSALS, MANUAL_BEFORE, MANUAL_AFTER, BASELINE} | set(CHILDREN)), "duplicate or colliding fold parent")
    io.require(isinstance(plan["reviewed_parent_manifest_ids"], dict) and set(plan["reviewed_parent_manifest_ids"]) == set(names), "every fold parent requires an explicitly reviewed manifest ID")
    for identity in plan["reviewed_parent_manifest_ids"].values(): contracts._hash(identity, "reviewed parent")
    for field in ("proposal_git_commit", "manual_git_commit"):
        contracts._expect_pattern(plan[field], field, contracts.GIT_COMMIT_RE, "full Git commit")
    io.exact(plan["bindings"], BINDINGS, "fold input bindings")
    for name, member in plan["bindings"].items():
        io.exact(member, {"source", "object"}, "fold member")
        expected_source, expected_object, _path = BINDINGS[name]
        io.require(member["source"] in names, "binding names an absent source")
        if expected_source is not None:
            io.require(member == {"source": expected_source, "object": expected_object}, "fold binding substitutes its reviewed source family")
    return plan


def physical(ref, roots):
    io.require(ref["root"] in roots, "missing fold parent physical root: " + ref["root"])
    contracts.validate_literal_relative_path(ref["path"], "fold member path")
    path = roots[ref["root"]] / ref["path"]
    io.real_path(path)
    return path


def capture_file(path, ref, *, retain=False):
    """Stream large ancestry objects; retain only immutable selected bytes."""
    io.real_path(path)
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        before = os.fstat(fd)
        io.require(stat.S_ISREG(before.st_mode) and before.st_nlink == 1 and before.st_size == ref["bytes"], "fold parent is not its exact regular file")
        io.require(before.st_size <= (512 * 1024 * 1024 if retain else 4 * 1024 * 1024 * 1024), "fold parent exceeds resource bound")
        digest, count, chunks = hashlib.sha256(), 0, []
        while raw := os.read(fd, 1024 * 1024):
            count += len(raw); digest.update(raw)
            io.require(count <= ref["bytes"], "fold parent grew while reading")
            if retain: chunks.append(raw)
        signature = lambda s: (s.st_dev, s.st_ino, s.st_size, s.st_mtime_ns, s.st_ctime_ns)
        io.require(signature(before) == signature(os.fstat(fd)) == signature(path.lstat()), "fold parent changed while reading")
        io.require(count == ref["bytes"] and digest.hexdigest() == ref["sha256"], "fold parent bytes differ from reviewed object")
        return b"".join(chunks) if retain else None
    finally:
        os.close(fd)


def checked(raw, ref):
    io.require(len(raw) == ref["bytes"] and io.sha(raw) == ref["sha256"], "curated parent differs from reviewed object")
    return raw


def capture_git(repository, commit, *, paths=None, proposal_scope=False):
    git = io.git_snapshot
    command = git._validated_git("/usr/bin/git")
    command_sha = io.sha(io.read(Path(command), executable=True))
    repository = git._validated_repository(repository)
    git._require_top_level(command, repository); git._reject_partial_clone(command, repository)
    io.require(git._run_git(command, repository, ["cat-file", "-t", commit]) == b"commit\n", "curation pin must name a native Git commit")
    tree = git._commit_tree(command, repository, commit)
    selected = list(paths or [])
    if proposal_scope:
        entries = git._tree_entries(command, repository, commit, "brain/proposals")
        proposals = [item.path for item in entries if Path(item.path).parent.as_posix() == "brain/proposals" and item.path.endswith(".jsonl")]
        io.require(1 <= len(proposals) <= 1000, "proposal Git scope has invalid shard count")
        selected.extend(proposals)
    io.require(selected == list(dict.fromkeys(selected)), "duplicate curated path")
    entries = []
    for path in sorted(selected):
        contracts.validate_literal_relative_path(path, "curated input path")
        members = git._select_entries(git._tree_entries(command, repository, commit, path), path, None)
        io.require(len(members) == 1 and members[0].path == path, "curation must select a regular native Git blob")
        entries.extend(members)
    values = {item.path: item.text.encode("utf-8") for item in git._read_text_blobs(command, repository, tuple(entries))}
    proof = {"commit": git._run_git(command, repository, ["cat-file", "commit", commit])}
    for path in sorted(selected):
        oid = tree
        for part in path.split("/")[:-1]:
            if oid not in proof: proof[oid] = git._run_git(command, repository, ["cat-file", "tree", oid])
            mode, oid = io.git_tree(proof[oid])[part]
            io.require(mode == "40000", "curated ancestor must be a Git tree")
        if oid not in proof: proof[oid] = git._run_git(command, repository, ["cat-file", "tree", oid])
    tool = {"schema": "wikilean.fold-curation-reader/v1", "sha256": command_sha,
        "version": git._run_git(command, repository, ["--version"]).decode("ascii").strip()}
    io.require(io.sha(io.read(Path(command), executable=True)) == command_sha, "Git reader changed during curation capture")
    verify_git(values, commit, tree, proof, proposal_scope=proposal_scope)
    return {"commit": commit, "tree": tree, "values": values, "proof": proof, "tool": tool}


def verify_git(values, commit, tree, proof, *, proposal_scope=False):
    io.require(io.git_oid("commit", proof["commit"]) == commit and proof["commit"].startswith(("tree " + tree + "\n").encode()), "curation commit/tree proof differs")
    used = {"commit"}
    for path, raw in values.items():
        oid, parts = tree, path.split("/")
        for index, part in enumerate(parts):
            io.require(oid in proof and io.git_oid("tree", proof[oid]) == oid, "curation tree proof differs")
            used.add(oid)
            mode, child = io.git_tree(proof[oid])[part]
            if index == len(parts) - 1:
                io.require(mode in {"100644", "100755"} and io.git_oid("blob", raw) == child, "curation is not its pinned regular Git blob")
            else:
                io.require(mode == "40000", "curation ancestor is not a tree")
                oid = child
    io.require(set(proof) == used, "curation proof contains undeclared objects")
    if proposal_scope:
        oid = tree
        for part in ("brain", "proposals"):
            _mode, oid = io.git_tree(proof[oid])[part]
        names = {"brain/proposals/" + name for name, (mode, _oid) in io.git_tree(proof[oid]).items() if name.endswith(".jsonl")}
        io.require(names == {path for path in values if path.startswith("brain/proposals/")}, "curation omits a proposal shard from the pinned directory")


def capture_curations(plan, roots):
    io.require(GIT_ROOT in roots, "native proposal Git root is required")
    repository = roots[GIT_ROOT]
    proposals = capture_git(repository, plan["proposal_git_commit"], paths=list(BASE_PATHS.values()), proposal_scope=True)
    baseline = capture_git(repository, plan["proposal_git_commit"], paths=list(COMPARE_PATHS.values()))
    after = capture_git(repository, plan["manual_git_commit"], paths=[CONTAINER_PATH])
    parents = [line.removeprefix(b"parent ").decode("ascii") for line in after["proof"]["commit"].split(b"\n\n", 1)[0].splitlines() if line.startswith(b"parent ")]
    io.require(len(parents) == 1, "manual curation requires one exact predecessor commit")
    before = capture_git(repository, parents[0], paths=[CONTAINER_PATH])
    return {PROPOSALS: proposals, BASELINE: baseline, MANUAL_AFTER: after, MANUAL_BEFORE: before}


def capture_parents(plan, roots):
    validate_plan(plan)
    sources = {s["source"]: copy.deepcopy(s) for s in plan["parents"]}
    manifests = {name: io.source_plan_contracts._source_manifest_from_plan(source, "fold parent") for name, source in sources.items()}
    io.require({name: m["source_manifest_id"] for name, m in manifests.items()} == plan["reviewed_parent_manifest_ids"], "fold parent differs from explicitly reviewed identity")
    by_id = {m["source_manifest_id"]: m for m in manifests.values()}
    io.require(len(by_id) == len(manifests), "duplicate fold parent identity")
    objects = {(name, obj["name"]): obj for name, source in sources.items() for obj in source["objects"]}
    selected = {(m["source"], m["object"]) for m in plan["bindings"].values()}
    io.require(selected <= set(objects), "fold selected input absent from parent")
    mathlib_key = (plan["bindings"]["mathlib"]["source"], "git_tree")
    raw_tree = capture_file(physical(objects[mathlib_key], roots), objects[mathlib_key], retain=True)
    tree = io.parse(raw_tree, "Mathlib tree index")
    mathlib = manifests[mathlib_key[0]]
    io.require(mathlib["source_kind"] == "acquired_dataset" and mathlib["pin"]["type"] == "git_commit" and tree["commit"] == mathlib["pin"]["value"], "fold requires the exact acquired Mathlib Git generation")
    mathlib_paths = {}
    for entry in tree["entries"]:
        if not entry["path"].startswith("Mathlib/"): continue
        io.require(entry["mode"] in {"100644", "100755"}, "Mathlib subtree contains unsupported nonregular content")
        key = (mathlib_key[0], "file-" + io.sha(entry["path"].encode()))
        io.require(key in objects and all(objects[key][field] == entry[field] for field in ("sha256", "bytes")), "Mathlib subtree object differs or is omitted")
        io.require(entry["path"] not in mathlib_paths, "duplicate Mathlib native path")
        mathlib_paths[entry["path"]] = key; selected.add(key)
    io.require(mathlib_paths, "empty Mathlib subtree")
    captured, lineages = {}, {}
    for name, source in sources.items():
        curated = None
        if source["source_kind"] == "curated_git_tree":
            root_names = {o["root"] for o in source["objects"]}
            io.require(len(root_names) == 1, "curated fold parent requires one native Git root")
            group = capture_git(roots[next(iter(root_names))], source["pin"]["value"], paths=[o["path"] for o in source["objects"]])
            io.require(group["tree"] == source["pin"]["tree"], "curated fold parent tree differs")
            curated = group["values"]
        for item in source["objects"]:
            key = (name, item["name"])
            value = checked(curated[item["path"]], item) if curated is not None else capture_file(physical(item, roots), item, retain=key in selected)
            if key in selected:
                io.require("normalized" in item["roles"], "fold data input must be normalized in its parent")
                captured[key] = value
        if curated is not None: continue
        evidence, receipts, preimages = source["evidence"], {}, {}
        for ref in evidence["acquisition_receipts"]:
            receipts[ref["acquisition_receipt_id"]] = io.parse(capture_file(physical(ref, roots), ref, retain=True), "parent receipt")
        ref = evidence["normalization_lineage"]
        lineage = io.parse(capture_file(physical(ref, roots), ref, retain=True), "parent lineage"); lineages[name] = lineage
        for ref in evidence["request_parameter_preimages"]:
            capture_file(physical(ref, roots), ref)
            preimages[ref["parameters_sha256"]] = {field: ref[field] for field in ("parameters_sha256", "bytes", "media_type")}
        io.require(set(lineage["parent_source_manifest_ids"]) <= set(by_id), "fold parent evidence ancestor is missing")
        contracts.validate_source_manifest_evidence_documents(manifests[name], receipts=receipts, lineage=lineage,
            request_parameter_preimages=preimages, parent_source_manifests={key: by_id[key] for key in lineage["parent_source_manifest_ids"]})
    io.require(mathlib["source_manifest_id"] in lineages["mathlib-docs"]["parent_source_manifest_ids"], "fold oracle does not descend from its selected Mathlib source")
    for name in ("grounding", "registry"):
        member = plan["bindings"][name]; source = sources[member["source"]]; obj = objects[(member["source"], member["object"])]
        io.require(source["source_kind"] == "curated_git_tree" and obj["path"] == BINDINGS[name][1], "fold curation input must use its exact native Git path")
    for name in ("hierarchy", "formal-conjectures"):
        io.require(manifests[plan["bindings"][name]["source"]]["source_kind"] == "sealed_snapshot", "fold catalog oracle must have derived-source lineage")
    io.require(manifests["wikidata-observation"]["source_kind"] == "acquired_dataset", "fold universe must be the acquired observation")
    pending = [manifests[name]["source_manifest_id"] for name, _obj in selected]
    seen = set()
    while pending:
        identity = pending.pop()
        if identity in seen: continue
        seen.add(identity); source_name = by_id[identity]["source"]
        if source_name in lineages: pending.extend(lineages[source_name]["parent_source_manifest_ids"])
    io.require(seen == set(by_id), "fold plan contains unrelated source ancestry")
    return sources, manifests, objects, captured, mathlib_paths


def manual_contributions(curations):
    before = curations[MANUAL_BEFORE]["values"][CONTAINER_PATH]
    after = curations[MANUAL_AFTER]["values"][CONTAINER_PATH]
    io.require(after.startswith(before) and before.endswith(b"\n"), "manual curation is not an exact append to its predecessor")
    addition = after[len(before):]
    rows = adapter.rows(addition)
    io.require(len(rows) == 5 and {(r.get("qid"), r.get("path")) for r in rows} == MANUAL_KEYS,
               "manual curation differs from the five reviewed contributions")
    io.require(not MANUAL_KEYS & {(r.get("qid"), r.get("path")) for r in adapter.rows(before)}, "manual contribution already existed in its predecessor")
    return addition


def compose_containers(proposal_raw, manual_raw, origins):
    projected, by_key, contributions = [], {}, []
    for raw, origin in zip((proposal_raw, manual_raw), origins, strict=True):
        for index, row in enumerate(adapter.rows(raw)):
            key = (row["qid"], row["path"])
            contributions.append({"source_manifest_id": origin, "row_index": index, "row_sha256": io.sha(io.artifact(row)), "qid": key[0], "path": key[1]})
            if key in by_key:
                io.require(io.artifact(by_key[key]) == io.artifact(row), "independent container contributions conflict; explicit review is required")
            else:
                projected.append(row); by_key[key] = row
    return b"".join(io.artifact(row) + b"\n" for row in projected), io.canonical({
        "schema": "wikilean.container-contribution-projection/v1", "contributions": contributions,
        "n_projection_rows": len(projected), "n_contributions": len(contributions)})


def semantic_delta(before, after, key):
    def indexed(raw):
        output = {}
        for row in adapter.rows(raw):
            if "_meta" in row: continue
            identity = key(row)
            io.require(identity not in output, "comparison input has duplicate row identities")
            output[identity] = row
        return output
    prior, current = indexed(before), indexed(after)
    return {"before": len(prior), "after": len(current),
        "added": [current[k] for k in sorted(current.keys() - prior.keys())],
        "removed": [prior[k] for k in sorted(prior.keys() - current.keys())],
        "changed": [{"before": prior[k], "after": current[k]} for k in sorted(prior.keys() & current.keys()) if io.artifact(prior[k]) != io.artifact(current[k])]}


def build_documents(plan, sources, manifests, objects, captured, mathlib_paths, curations, profile, programs, when):
    generation = profile_generation(profile)
    configuration = CONFIGURATION_V1 if generation == 1 else CONFIGURATION_V2
    io.require(profile in profiles()["profiles"] and profile["files"] ==
        [{"path": path, "sha256": io.sha(raw)} for path, raw in sorted(programs.items())], "unreviewed fold program preimages")
    # Work on new mappings: verification never mutates the reviewed parent plan.
    sources, manifests, objects, captured = (copy.deepcopy(sources), copy.deepcopy(manifests), copy.deepcopy(objects), dict(captured))
    files = {"plan.json": io.canonical(plan), "normalization/profile.json": io.canonical(profile),
        "normalization/configuration.json": io.canonical(configuration)}
    files.update({"implementation/" + path: raw for path, raw in programs.items()})

    def planned(name, path, roles, media="application/json"):
        raw = files[path]
        if generation == 2 and not raw:
            media = "application/octet-stream"
        item = {"name": name, "root": PHYSICAL_ROOT, "path": path, "sha256": io.sha(raw), "bytes": len(raw),
            "media_type": media, "roles": sorted(roles), "redistribution": "restricted"}
        files.setdefault("objects/sha256/" + item["sha256"], raw)
        return item

    tool = {"name": "wikilean-proposal-fold-normalizer", "version": str(generation), "sha256": io.sha(files["normalization/profile.json"])}
    support = [planned("normalizer-profile", "normalization/profile.json", ["receipt"]),
        planned("normalizer-configuration", "normalization/configuration.json", ["receipt"]),
        planned("normalizer-plan", "plan.json", ["receipt"])]
    support += [planned("normalizer-program-" + str(index), "implementation/" + path, ["receipt"], "text/x-python")
        for index, path in enumerate(sorted(programs))]
    for source_name, group in sorted(curations.items()):
        verify_git(group["values"], group["commit"], group["tree"], group["proof"], proposal_scope=source_name == PROPOSALS)
        object_names = {}
        for path in group["values"]:
            if source_name == PROPOSALS:
                name = next((name for name, native in BASE_PATHS.items() if native == path), "proposal-" + io.sha(path.encode()))
            elif source_name == BASELINE:
                name = next(name for name, native in COMPARE_PATHS.items() if native == path)
            else:
                name = "manual-container-before" if source_name == MANUAL_BEFORE else "manual-container-after"
            object_names[path] = name
        curated_objects = []
        for path, raw in sorted(group["values"].items()):
            name = object_names[path]
            private_path = "curation/" + source_name + "/files/" + path
            files[private_path] = raw
            item = planned(name, private_path, ["normalized", "raw"], "application/x-ndjson")
            item.update(root=GIT_ROOT, path=path)
            curated_objects.append(item)
            objects[(source_name, name)] = item; captured[(source_name, name)] = raw
        for name, raw in {**group["proof"], "reader.json": io.canonical(group["tool"])}.items():
            path = "curation/" + source_name + "/proof/" + name
            files[path] = raw
            support.append(planned("curation-proof-" + io.sha((source_name + "/" + name).encode()), path, ["receipt"], "application/octet-stream"))
        acquisition = {"name": "git-native-curation-reader", "version": "1", "sha256": group["tool"]["sha256"]}
        source = {"source": source_name, "source_kind": "curated_git_tree", "pin": {"type": "git_commit", "value": group["commit"], "tree": group["tree"]},
            "objects": sorted(curated_objects, key=lambda item: item["name"]),
            "license": {"expression": "CC0-1.0", "redistribution": "restricted", "notice": "Native WikiLean curation bytes; no new upstream acquisition or public release is claimed."},
            "acquisition": acquisition, "normalization": {"schema": f"wikilean.native-fold-curation/v{generation}", "tool": acquisition,
                "inputs": sorted(item["name"] for item in curated_objects), "outputs": sorted(item["name"] for item in curated_objects)}}
        sources[source_name] = source
        manifests[source_name] = io.source_plan_contracts._source_manifest_from_plan(source, "native fold curation")
        files["source-manifests/" + source_name + ".json"] = io.canonical(manifests[source_name])

    def child(source_name, output, selected):
        raw_objects, inputs, normalized = [], [], []
        for key in sorted(selected):
            original = objects[key]
            io.require("normalized" in original["roles"], "fold lineage input is not normalized in its parent")
            path = "inputs/sha256/" + original["sha256"]
            files[path] = captured[key]
            item = planned(original["name"], path, ["raw"], original["media_type"])
            raw_objects.append(item)
            inputs.append({**io.object_ref(item), "origin": {"kind": "source_manifest", "id": manifests[key[0]]["source_manifest_id"]}})
        io.require(len({item["name"] for item in raw_objects}) == len(raw_objects), "fold parent object names collide")
        for name, (raw, suffix) in sorted(output.items()):
            path = "normalized/" + source_name + "/" + name + suffix
            files[path] = raw
            item = planned(name, path, ["normalized"], "application/x-ndjson" if suffix == ".jsonl" else "application/json")
            normalized.append(item)
        parents = {manifests[key[0]]["source_manifest_id"]: manifests[key[0]] for key in selected}
        schema = "wikilean." + source_name + f"/v{generation}"
        lineage = {"schema": contracts.NORMALIZATION_LINEAGE_SCHEMA_V1, "normalization_lineage_id": "sha256:" + "0" * 64,
            "source": source_name, "mode": "transform", "acquisition_receipt_ids": [], "parent_source_manifest_ids": sorted(parents),
            "normalization_schema": schema, "configuration_sha256": io.sha(files["normalization/configuration.json"]), "tool": tool,
            "inputs": sorted(inputs, key=lambda item: (item["origin"]["kind"], item["origin"]["id"], item["object"])),
            "outputs": [io.object_ref(item) for item in sorted(normalized, key=lambda item: item["name"])],
            "result": "complete", "audit": {"normalized_at": when}}
        lineage["normalization_lineage_id"] = contracts.normalization_lineage_identity(lineage)
        path = "evidence/" + source_name + ".json"; files[path] = io.canonical(lineage)
        source = {"source": source_name, "source_kind": "sealed_snapshot", "pin": {"type": "dataset_revision", "value": lineage["normalization_lineage_id"]},
            "objects": sorted([*raw_objects, *normalized, *support], key=lambda item: item["name"]),
            "license": {"expression": "CC0-1.0", "redistribution": "restricted", "notice": "Candidate fold source; original parent licenses remain attached. Public release and graph baseline are not approved."},
            "acquisition": tool, "normalization": {"schema": schema, "tool": tool,
                "inputs": sorted(item["name"] for item in raw_objects), "outputs": sorted(output)},
            "evidence": {"acquisition_receipts": [], "request_parameter_preimages": [], "normalization_lineage": {
                "root": PHYSICAL_ROOT, "path": path, "sha256": io.sha(files[path]), "bytes": len(files[path]), "media_type": "application/json",
                "normalization_lineage_id": lineage["normalization_lineage_id"]}}}
        manifest = io.source_plan_contracts._source_manifest_from_plan(source, "derived fold source")
        contracts.validate_source_manifest_evidence_documents(manifest, receipts={}, lineage=lineage,
            request_parameter_preimages={}, parent_source_manifests=parents)
        sources[source_name] = source; manifests[source_name] = manifest
        files["source-manifests/" + source_name + ".json"] = io.canonical(manifest)
        for item in normalized:
            objects[(source_name, item["name"])] = item
            captured[(source_name, item["name"])] = files[item["path"]]

    selected = {(member["source"], member["object"]) for member in plan["bindings"].values()} | set(mathlib_paths.values())
    selected.update(key for key in objects if key[0] == PROPOSALS)
    inputs = dict(curations[PROPOSALS]["values"])
    for name, member in plan["bindings"].items():
        path = BINDINGS[name][2]
        if path is not None: inputs[path] = captured[(member["source"], member["object"])]
    mathlib = {path: captured[key] for path, key in mathlib_paths.items()}
    folded, audit = adapter.fold(programs["brain/fold_proposals.py"], inputs, mathlib)
    output_names = {"proposal-containers": CONTAINER_PATH, "discovery-proposals": "brain/data/discovery_proposals.jsonl",
        "fc-links": "brain/data/fc_links.jsonl", "rejections": "brain/data/discovery_rejected.jsonl", "grading-disputes": "brain/data/grading_disputes.jsonl",
        "grounding-overrides": "catalog/data/grounding_overrides.jsonl", "universe-extension": "catalog/data/universe_extension.jsonl", "entity-request-plan": "request-plan.json"}
    child(FOLD, {**{name: (folded[path], ".json" if name == "entity-request-plan" else ".jsonl") for name, path in output_names.items()},
        "fold-audit": (io.canonical(audit), ".json")}, selected)
    manual = manual_contributions(curations)
    child(MANUAL, {"manual-containers": (manual, ".jsonl")}, {(MANUAL_BEFORE, "manual-container-before"), (MANUAL_AFTER, "manual-container-after")})
    combined, contributions = compose_containers(folded[CONTAINER_PATH], manual, [manifests[name]["source_manifest_id"] for name in (FOLD, MANUAL)])
    child(COMPOSED, {"container-links": (combined, ".jsonl"), "container-contributions": (contributions, ".json")},
        {(FOLD, "proposal-containers"), (MANUAL, "manual-containers")})

    # The prior FC file participates only in this separate comparison source.
    # Reconstructing all rows without it is a hard requirement for this v1 export.
    prior = curations[BASELINE]["values"]
    seeded, seed_audit = adapter.fold(programs["brain/fold_proposals.py"], inputs, mathlib,
        comparison_fc_seed=prior["brain/data/fc_links.jsonl"])
    io.require(seeded == folded, "opaque prior FC contribution survives; proposal provenance cannot be asserted")
    delta = {
        "containers": semantic_delta(prior[CONTAINER_PATH], combined, lambda row: (row["qid"], row["path"])),
        "discovery": semantic_delta(prior["brain/data/discovery_proposals.jsonl"], folded["brain/data/discovery_proposals.jsonl"], lambda row: (row["src"], row["dst"], row["kind"])),
        "fc": semantic_delta(prior["brain/data/fc_links.jsonl"], folded["brain/data/fc_links.jsonl"], lambda row: (row["qid"], row["decl"])),
    }
    names = {row["decl"] for row in adapter.rows(inputs["catalog/data/formal_conjectures.jsonl"]) if "decl" in row}
    rejected = adapter.rows(folded["brain/data/discovery_rejected.jsonl"])
    retractions = []
    for removed in delta["fc"]["removed"]:
        evidence = []
        for row in rejected:
            if row.get("action") != "fc_link" or row.get("verdict") != "reject" or not isinstance(row.get("decl"), str): continue
            decl = row["decl"]
            if decl not in names:
                matches = [name for name in names if name.endswith("." + decl)]
                if len(matches) == 1: decl = matches[0]
            if (row.get("qid"), decl) == (removed["qid"], removed["decl"]): evidence.append(row)
        io.require(evidence, "removed FC row lacks an explicit corresponding rejection decision")
        retractions.append({"removed": removed, "rejection_evidence": evidence})
    report = {"schema": "wikilean.proposal-fold-comparison/v1", "authority_baseline_approved": False,
        "note": "Provenance reconstruction is separate from approval of the graph delta; no production or plan binding changed.",
        "empty_fc_seed_reproduces_seeded_fold": True, "manual_contributions": 5,
        "delta": delta, "explicit_fc_retractions": retractions,
        "original_decisions_source_manifest_id": manifests[PROPOSALS]["source_manifest_id"], "seeded_fold_audit": seed_audit}
    comparison_inputs = selected | {(BASELINE, name) for name in COMPARE_PATHS} | {(MANUAL, "manual-containers"),
        (COMPOSED, "container-links"), (FOLD, "proposal-containers"), (FOLD, "discovery-proposals"), (FOLD, "fc-links"), (FOLD, "rejections")}
    child(COMPARISON, {"fold-comparison": (io.canonical(report), ".json")}, comparison_inputs)
    fragment = {"schema": "wikilean.proposal-fold-fragment/v1", "scope": "candidate-source-plan-fragment", "physical_root": PHYSICAL_ROOT,
        "source_publishable": False, "redistribution": "restricted", "authority_bindings_changed": False,
        "sources": sorted(sources.values(), key=lambda source: source["source"]),
        "candidate_outputs": {"brain-container-links": {"source": COMPOSED, "object": "container-links"},
            "brain-discovery-proposals": {"source": FOLD, "object": "discovery-proposals"},
            "brain-fc-links": {"source": FOLD, "object": "fc-links"},
            "grounding-overrides": {"source": FOLD, "object": "grounding-overrides"},
            "universe-extension": {"source": FOLD, "object": "universe-extension"}},
        "explicit_absence": ["brain-ext-anchor-links", "tauceti-links"]}
    files["source-fragment.json"] = io.canonical(fragment)
    export_schema = EXPORT_SCHEMAS[generation]
    document = {"schema": export_schema, "normalization_profile_id": profile["profile_id"], "normalized_at": when,
        "source_manifest_ids": sorted(manifests[name]["source_manifest_id"] for name in CHILDREN),
        "files": {path: {"sha256": io.sha(raw), "bytes": len(raw)} for path, raw in sorted(files.items())}}
    document["export_id"] = contracts.domain_hash(export_schema, document)
    files["export.json"] = io.canonical(document)
    return files
