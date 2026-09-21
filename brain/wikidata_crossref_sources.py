"""Offline entity-to-crossref evidence with a verified acquisition and Git parent.

Wikibase truthy semantics are specified at
https://www.mediawiki.org/wiki/Wikibase/Indexing/RDF_Dump_Format#Truthy_statements
Rank selection precedes value filtering: preferred novalue/somevalue statements
also suppress normal statements. Only concrete external-ID strings become xrefs.
The original requested QID remains the key after a verified entity redirect.
"""
from __future__ import annotations

import hashlib
import os
import re
import stat
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
for directory in (ROOT / "brain", ROOT / "brain/tools", ROOT / "brain/ingest"):
    sys.path.append(str(directory))
import authority_contracts as contracts
import source_plan_contracts
import wikidata_entity_bundle as entities
import git_snapshot
import stage_io

EXPORT_SCHEMA = "wikilean.wikidata-crossref-source-export/v1"
PROFILE_SCHEMA = "wikilean.wikidata-crossref-source-profiles/v1"
NORMALIZATION_SCHEMA = "wikilean.wikidata-truthy-crossrefs/v1"
CLAIMS_SCHEMA = "wikilean.wikidata-requested-entity-claims/v1"
SCOPE_SCHEMA = "wikilean.wikidata-crossref-scope/v1"
REGISTRY = ROOT / "brain/wikidata_crossref_profiles.json"
REGISTRY_PATH = "catalog/data/source_registry.json"
PHYSICAL_ROOT = "wikidata_crossref_export"
GIT_ROOT = "wikilean_git"
CURATED_SOURCE = "wikilean-crossref-registry"
TOOL_FILES = tuple(sorted(("brain/wikidata_crossref_sources.py", "brain/export_wikidata_crossrefs.py",
    "brain/wikidata_entity_bundle.py", "brain/ingest/git_snapshot.py", "brain/stage_io.py",
    "brain/tools/authority_contracts.py", "brain/tools/execution_environment.py", "brain/tools/source_plan_contracts.py")))
MAX_FILE = 512 * 1024 * 1024


class ExportError(RuntimeError):
    pass


def require(condition, message):
    if not condition:
        raise ExportError(message)


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def canonical(value):
    return contracts.canonical_json_bytes(value)


def artifact(value):
    return contracts.canonical_artifact_json_bytes(value)


def parse(raw, label, *, data=False):
    return (contracts.parse_artifact_json_bytes if data else contracts.parse_json_bytes)(raw, location=label)


def exact(value, keys, label):
    require(isinstance(value, dict) and set(value) == set(keys), label + ": unexpected fields")
    return value


def real_path(path):
    require(path.is_absolute() and ".." not in path.parts and not any(p.is_symlink() for p in (path, *path.parents)),
            "evidence paths require real absolute ancestry")


def read(path, *, private=False, executable=False):
    real_path(path)
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        before = os.fstat(fd)
        require(stat.S_ISREG(before.st_mode) and (before.st_nlink == 1 or executable), "expected regular single-link file")
        require(not private or (before.st_uid == os.getuid() and stat.S_IMODE(before.st_mode) == 0o644),
                "private file ownership or mode differs")
        require(before.st_size <= MAX_FILE, "evidence member exceeds bound")
        chunks, count = [], 0
        while raw := os.read(fd, 1024 * 1024):
            count += len(raw)
            require(count <= MAX_FILE, "evidence member grew beyond bound")
            chunks.append(raw)
        signature = lambda s: (s.st_dev, s.st_ino, s.st_size, s.st_mtime_ns, s.st_ctime_ns)
        require(signature(before) == signature(os.fstat(fd)) == signature(path.lstat()), "evidence changed while reading")
        return b"".join(chunks)
    finally:
        os.close(fd)


def origins():
    for module, relative in ((contracts, "brain/tools/authority_contracts.py"),
        (contracts.execution_environment_contract, "brain/tools/execution_environment.py"),
        (source_plan_contracts, "brain/tools/source_plan_contracts.py"), (entities, "brain/wikidata_entity_bundle.py"),
        (git_snapshot, "brain/ingest/git_snapshot.py"), (stage_io, "brain/stage_io.py")):
        require(Path(getattr(module, "__file__", "")).resolve() == ROOT / relative,
                "unexpected helper module origin: " + relative)
    require(source_plan_contracts.contracts is contracts and entities.contracts is contracts,
            "helpers loaded a different authority module")


origins()
LOADED = {path: sha(read(ROOT / path)) for path in TOOL_FILES if path != "brain/export_wikidata_crossrefs.py"}


def profile_id(profile):
    return contracts.domain_hash("wikilean.wikidata-crossref-source-profile.v1", {"files": profile["files"]})


def profiles():
    raw = read(REGISTRY)
    value = exact(parse(raw, "profiles"), {"schema", "current_profile", "profiles"}, "profiles")
    require(raw == canonical(value) and value["schema"] == PROFILE_SCHEMA and isinstance(value["profiles"], list),
            "invalid reviewed profile registry")
    ids = []
    for profile in value["profiles"]:
        exact(profile, {"profile_id", "files"}, "profile")
        require(isinstance(profile["files"], list), "profile files must be an array")
        paths = []
        for item in profile["files"]:
            exact(item, {"path", "sha256"}, "helper")
            contracts.validate_literal_relative_path(item["path"], "helper path")
            contracts._digest(item["sha256"], "helper digest")
            paths.append(item["path"])
        require(paths == sorted(set(paths)) and "brain/wikidata_crossref_sources.py" in paths and
                profile_id(profile) == profile["profile_id"], "invalid whole generation")
        ids.append(profile["profile_id"])
    require(ids == sorted(set(ids)) and value["current_profile"] in ids, "invalid current profile")
    return value


def current_profile():
    origins()
    registry = profiles()
    profile = next(p for p in registry["profiles"] if p["profile_id"] == registry["current_profile"])
    actual = {path: sha(read(ROOT / path)) for path in TOOL_FILES}
    require(profile["files"] == [{"path": path, "sha256": actual[path]} for path in TOOL_FILES],
            "current implementation differs from reviewed whole generation")
    require(all(actual[path] == digest for path, digest in LOADED.items()), "loaded helper bytes changed since import")
    return profile


def write(root, path, raw):
    contracts.validate_literal_relative_path(path, "output path")
    target = root / path
    stage_io.ensure_private_directory(root, target.parent)
    stage_io.write_bytes_exclusive(target, raw, mode=0o644)


def capture_tree(root):
    real_path(root)
    files, directories, identities = {}, set(), {}
    for current, names, members in os.walk(root, followlinks=False):
        path = Path(current)
        real_path(path)
        metadata = path.lstat()
        require(stat.S_ISDIR(metadata.st_mode) and metadata.st_uid == os.getuid() and stat.S_IMODE(metadata.st_mode) == 0o700,
                "bundle directories must be private and owned")
        identities[path] = metadata
        for name in names:
            child = path / name
            require(not child.is_symlink() and child.is_dir(), "bundle directory substitution")
            directories.add(child.relative_to(root).as_posix())
        for name in members:
            relative = (path / name).relative_to(root).as_posix()
            contracts.validate_literal_relative_path(relative, "bundle member")
            files[relative] = read(path / name, private=True)
    require(directories == {p.as_posix() for name in files for p in Path(name).parents if p != Path(".")},
            "bundle contains undeclared empty directories")
    for path, before in identities.items():
        after = path.lstat()
        require(os.path.samestat(before, after) and before.st_mtime_ns == after.st_mtime_ns and
                before.st_ctime_ns == after.st_ctime_ns, "bundle directory changed during capture")
    return files


def verify_captured_bundle(captured, *, scratch_parent=None):
    """Verify exactly the bytes supplied to reduction, with no second ambient read."""
    manifest = parse(captured["bundle.json"], "entity bundle")
    contracts._hash(manifest["bundle_id"], "bundle ID")
    with tempfile.TemporaryDirectory(prefix=".crossref-verify-", dir=scratch_parent) as temporary:
        anchor = Path(temporary).resolve()
        root = anchor / manifest["bundle_id"].removeprefix("sha256:")
        stage_io.ensure_private_directory(anchor, root)
        for path, raw in captured.items():
            write(root, path, raw)
        return entities.verify_wikidata_entity_bundle(root)


def git_oid(kind, raw):
    return hashlib.sha1(kind.encode() + b" " + str(len(raw)).encode() + b"\0" + raw).hexdigest()


def git_tree(raw):
    output = {}
    while raw:
        header, separator, tail = raw.partition(b"\0")
        require(bool(separator) and len(tail) >= 20, "truncated Git tree proof")
        mode, separator, name = header.partition(b" ")
        require(bool(separator), "invalid Git tree header")
        name, mode = name.decode("utf-8"), mode.decode("ascii")
        require(name not in output and name not in {"", ".", ".."} and "/" not in name, "invalid Git tree name")
        output[name] = (mode, tail[:20].hex())
        raw = tail[20:]
    return output


def verify_git(registry, commit, tree, proof):
    contracts._expect_pattern(commit, "curated commit", contracts.GIT_COMMIT_RE, "full Git commit")
    contracts._expect_pattern(tree, "curated tree", contracts.GIT_COMMIT_RE, "full Git tree")
    require(git_oid("commit", proof["commit"]) == commit and proof["commit"].startswith(("tree " + tree + "\n").encode()),
            "curated commit/tree proof differs")
    oid, used = tree, {"commit"}
    for index, part in enumerate(REGISTRY_PATH.split("/")):
        require(oid in proof and git_oid("tree", proof[oid]) == oid, "curated Git tree proof differs")
        used.add(oid)
        entries = git_tree(proof[oid])
        require(part in entries, "source registry absent from pinned Git tree")
        mode, child = entries[part]
        if index == 2:
            require(mode in {"100644", "100755"} and git_oid("blob", registry) == child,
                    "source registry differs from pinned regular Git blob")
        else:
            require(mode == "40000", "source registry ancestor is not a Git tree")
            oid = child
    require(set(proof) == used, "curated Git proof contains undeclared objects")


def capture_git(repository, commit):
    command = git_snapshot._validated_git("/usr/bin/git")
    executable_sha = sha(read(Path(command), executable=True))
    repository = git_snapshot._validated_repository(repository)
    git_snapshot._require_top_level(command, repository)
    git_snapshot._reject_partial_clone(command, repository)
    contracts._expect_pattern(commit, "curated commit", contracts.GIT_COMMIT_RE, "full Git commit")
    require(git_snapshot._run_git(command, repository, ["cat-file", "-t", commit]) == b"commit\n", "curation pin is not a commit")
    tree = git_snapshot._commit_tree(command, repository, commit)
    proof = {"commit": git_snapshot._run_git(command, repository, ["cat-file", "commit", commit])}
    oid = tree
    for part in (None, "catalog", "data"):
        if part is not None:
            mode, oid = git_tree(proof[oid])[part]
            require(mode == "40000", "curated ancestor is not a tree")
        proof[oid] = git_snapshot._run_git(command, repository, ["cat-file", "tree", oid])
    mode, blob = git_tree(proof[oid])["source_registry.json"]
    require(mode in {"100644", "100755"}, "registry is not a regular Git blob")
    raw = git_snapshot._run_git(command, repository, ["cat-file", "blob", blob])
    verify_git(raw, commit, tree, proof)
    tool = {"schema": "wikilean.curated-git-reader/v1", "git_sha256": executable_sha,
            "git_version": git_snapshot._run_git(command, repository, ["--version"]).decode("ascii").strip()}
    require(sha(read(Path(command), executable=True)) == executable_sha, "Git executable changed during curation capture")
    return raw, tree, proof, tool


def claim_map(captured, verified):
    """Replay successful response claims, retaining the verified requested aliases."""
    records = entities._read_transcript(captured["acquired.jsonl"],
        {path: raw for path, raw in captured.items() if path.startswith("requests/")})
    output = {}
    for record in records:
        payload = parse(record["response"], "entity response", data=True)
        if "error" in payload:
            if len(record["qids"]) == 1:
                output[record["qids"][0]] = {"missing": True}
            continue  # Full acquisition verification already checked bisection and error binding.
        resolved = entities._normalize_success_payload(payload, record["qids"], location="verified entity response")
        for requested, metadata in resolved.items():
            require(requested not in output, "duplicate successful requested QID")
            if metadata.get("missing"):
                output[requested] = {"missing": True}
            else:
                row = payload["entities"].get(requested, payload["entities"].get(metadata["qid"]))
                require(isinstance(row, dict) and row["id"] == metadata["qid"], "resolved claims differ from entity identity")
                output[requested] = {"requested": requested, "qid": metadata["qid"], "claims": row.get("claims", {})}
            require(metadata == verified.entities[requested], "claims and original entity normalization differ")
    require(sorted(output) == list(verified.requested_qids), "claim map does not cover exact requested scope")
    return {"schema": CLAIMS_SCHEMA, "entities": output}


def properties(registry):
    sources = parse(registry, "curated source registry", data=True).get("crossref_sources")
    require(isinstance(sources, dict), "registry requires crossref_sources")
    result = {}
    for key, entry in sorted(sources.items()):
        require(isinstance(key, str) and re.fullmatch(r"[a-z][a-z0-9_]*", key) and isinstance(entry, dict), "invalid crossref registry entry")
        declared = entry.get("wikidata_property")
        if declared is None or declared == "":
            continue
        require(isinstance(declared, str), "Wikidata property declaration must be a string or null")
        for pid in declared.split("/"):
            require(bool(re.fullmatch(r"P[1-9][0-9]*", pid)), "invalid registered Wikidata property")
            result.setdefault(pid, set()).add(key)
    require(bool(result), "registry has no crossref properties")
    return {pid: sorted(keys) for pid, keys in sorted(result.items())}


def crossrefs(claims, registry):
    exact(claims, {"schema", "entities"}, "claims")
    require(claims["schema"] == CLAIMS_SCHEMA and isinstance(claims["entities"], dict), "invalid claims map")
    props, xrefs = properties(registry), {}
    for requested, entity in sorted(claims["entities"].items()):
        require(isinstance(requested, str) and entities.QID_RE.fullmatch(requested), "invalid requested QID")
        if entity == {"missing": True}:
            continue
        exact(entity, {"requested", "qid", "claims"}, "claimed entity")
        require(entity["requested"] == requested and isinstance(entity["qid"], str) and entities.QID_RE.fullmatch(entity["qid"])
                and isinstance(entity["claims"], dict), "invalid claimed entity identity")
        found = {}
        for pid, keys in props.items():
            rows = entity["claims"].get(pid, [])
            require(isinstance(rows, list), "registered property statements must be an array")
            for row in rows:
                require(isinstance(row, dict) and row.get("rank") in {"preferred", "normal", "deprecated"},
                        "registered statement lacks a valid rank")
            rank = "preferred" if any(row["rank"] == "preferred" for row in rows) else "normal"
            for row in rows:
                if row["rank"] != rank:
                    continue
                snak = row.get("mainsnak")
                require(isinstance(snak, dict) and snak.get("property") == pid and
                        snak.get("snaktype") in {"value", "novalue", "somevalue"}, "invalid registered statement snak")
                if snak["snaktype"] != "value" or snak.get("datatype") != "external-id":
                    continue
                value = snak.get("datavalue")
                require(isinstance(value, dict), "external-ID value lacks datavalue")
                if value.get("type") != "string" or not isinstance(value.get("value"), str) or not value["value"]:
                    continue
                for key in keys:
                    found.setdefault(key, set()).add(value["value"])
        if found:
            xrefs[requested] = {key: sorted(values) for key, values in sorted(found.items())}
    return {"fetched_from": entities.UPSTREAM_URI, "properties": props, "xrefs": xrefs}


def acquisition_programs(captured):
    tool = entities._validate_toolchain(parse(captured["toolchain.json"], "acquisition tool"))
    return {"brain/acquire_wikidata_entities.py": tool["wrapper"]["sha256"],
            **{item["path"]: item["sha256"] for item in tool["local_dependencies"]}}


def object_ref(item):
    return {"object": item["name"], **{key: item[key] for key in ("sha256", "bytes", "media_type")}}


def build_export(captured, verified, registry, commit, tree, proof, git_tool, profile, programs, acquisition_sources, when):
    """Reconstruct all source/evidence bytes using already captured immutable inputs."""
    require(profile in profiles()["profiles"], "unreviewed normalization generation")
    require(profile["files"] == [{"path": path, "sha256": sha(raw)} for path, raw in sorted(programs.items())],
            "normalizer program preimages differ from whole profile")
    require({path: sha(raw) for path, raw in acquisition_sources.items()} == acquisition_programs(captured),
            "acquisition program preimages differ from reviewed captured toolchain")
    verify_git(registry, commit, tree, proof)
    exact(git_tool, {"schema", "git_sha256", "git_version"}, "curated Git tool")
    require(git_tool["schema"] == "wikilean.curated-git-reader/v1" and isinstance(git_tool["git_version"], str)
            and git_tool["git_version"].startswith("git version "), "invalid curated Git reader metadata")
    contracts._digest(git_tool["git_sha256"], "curated Git executable digest")
    receipt = parse(captured["acquisition-receipt.json"], "acquisition receipt")
    old_lineage = parse(captured["normalization-lineage.json"], "original normalization lineage")
    bundle = parse(captured["bundle.json"], "entity bundle")
    prefix = "acquisition/" + bundle["bundle_id"].removeprefix("sha256:") + "/"
    files = {prefix + path: raw for path, raw in captured.items()}
    files.update({"implementation/" + path: raw for path, raw in programs.items()})
    files.update({"acquisition-implementation/" + path: raw for path, raw in acquisition_sources.items()})
    files.update({"curated/proof/" + name: raw for name, raw in proof.items()})
    files["curated/source_registry.json"], files["curated/git-tool.json"] = registry, canonical(git_tool)
    files["normalization/profile.json"] = canonical(profile)
    tool = {"name": "wikilean-wikidata-crossref-normalizer", "version": "1", "sha256": sha(canonical(profile))}
    config = {"schema": NORMALIZATION_SCHEMA, "claims_schema": CLAIMS_SCHEMA,
        "rank": "preferred-if-any-else-normal; exclude-deprecated; select-rank-before-values",
        "values": "nonempty-concrete-string-external-id; exact-unicode; sorted-unique",
        "properties": "all-registry-keys-per-property; slash-separated-property-union",
        "redirects": "requested-key-and-verified-canonical-claims; no-extra-canonical-keys",
        "missing": "explicit-queried-scope; omit-empty-xrefs", "scope": "exact-original-request-plan",
        "upstream_consistency": "independent-live-requests/no-snapshot", "registry_sha256": sha(registry)}
    files["normalization/configuration.json"] = canonical(config)
    claimed = claim_map(captured, verified)
    normalized = crossrefs(claimed, registry)
    files["normalized/entity_claims.json"] = artifact(claimed)
    files["normalized/wikidata_crossrefs.json"] = artifact(normalized)
    files["normalized/requested_qid_scope.json"] = canonical({"schema": SCOPE_SCHEMA, "qids": list(verified.requested_qids)})

    def ref(path, media="application/json"):
        raw = files[path]
        return {"root": PHYSICAL_ROOT, "path": path, "sha256": sha(raw), "bytes": len(raw), "media_type": media}

    def planned(name, path, roles, media="application/json"):
        item = {"name": name, **ref(path, media), "roles": sorted(roles), "redistribution": "restricted"}
        files.setdefault("objects/sha256/" + item["sha256"], files[path])
        return item

    def evidence(path, **ids):
        return {**ref(path), **ids}

    support = [planned("normalizer_profile", "normalization/profile.json", ["receipt"]),
               planned("normalizer_configuration", "normalization/configuration.json", ["receipt"])]
    support += [planned("normalizer_program_" + str(i), "implementation/" + path, ["receipt"], "text/x-python")
                for i, path in enumerate(sorted(programs))]
    raw = planned("wikidata_raw", prefix + "acquired.jsonl", ["raw"], "application/x-ndjson")
    parent_outputs = [planned("entities", prefix + entities.NORMALIZED_PATH, ["normalized"]),
                      planned("entity_claims", "normalized/entity_claims.json", ["normalized"])]

    def lineage(source, inputs, outputs, parents, receipts, path, schema):
        value = {"schema": contracts.NORMALIZATION_LINEAGE_SCHEMA_V1, "normalization_lineage_id": "sha256:" + "0" * 64,
            "source": source, "mode": "transform", "acquisition_receipt_ids": sorted(receipts),
            "parent_source_manifest_ids": sorted(parents), "normalization_schema": schema,
            "configuration_sha256": sha(files["normalization/configuration.json"]), "tool": tool,
            "inputs": sorted(inputs, key=lambda item: (item["origin"]["kind"], item["origin"]["id"], item["object"])),
            "outputs": [object_ref(item) for item in sorted(outputs, key=lambda item: item["name"])],
            "result": "complete", "audit": {"normalized_at": when}}
        value["normalization_lineage_id"] = contracts.normalization_lineage_identity(value)
        contracts.validate_normalization_lineage(value)
        files[path] = canonical(value)
        return value

    parent_lineage = lineage("wikidata-wbgetentities", [{**object_ref(raw),
        "origin": {"kind": "acquisition_receipt", "id": receipt["acquisition_receipt_id"]}}],
        parent_outputs, [], [receipt["acquisition_receipt_id"]], "evidence/entity-claims-lineage.json", CLAIMS_SCHEMA)
    acq_support = [planned("acquisition_tool", prefix + "toolchain.json", ["receipt"]),
        planned("original_request_plan", prefix + "request-plan.json", ["receipt"]),
        planned("original_normalization_lineage", prefix + "normalization-lineage.json", ["receipt"]),
        planned("original_bundle", prefix + "bundle.json", ["receipt"])]
    acq_support += [planned("acquisition_program_" + str(i), "acquisition-implementation/" + path, ["receipt"], "text/x-python")
                    for i, path in enumerate(sorted(acquisition_sources))]
    parent = {"source": "wikidata-wbgetentities", "source_kind": "acquired_dataset", "pin": receipt["pin"],
        "objects": sorted([raw, *parent_outputs, *support, *acq_support], key=lambda item: item["name"]),
        "license": {"expression": "LicenseRef-Wikidata-Private-Audit", "redistribution": "restricted",
                    "notice": "Public Wikidata acquisition retained for private migration review; no publication approval asserted."},
        "acquisition": receipt["tool"], "normalization": {"schema": CLAIMS_SCHEMA, "tool": tool,
            "inputs": ["wikidata_raw"], "outputs": ["entities", "entity_claims"]},
        "evidence": {"acquisition_receipts": [evidence(prefix + "acquisition-receipt.json",
            acquisition_receipt_id=receipt["acquisition_receipt_id"])],
            "normalization_lineage": evidence("evidence/entity-claims-lineage.json",
                normalization_lineage_id=parent_lineage["normalization_lineage_id"]),
            "request_parameter_preimages": sorted([{**ref(prefix + path, entities.REQUEST_MEDIA_TYPE), "parameters_sha256": sha(data)}
                for path, data in captured.items() if path.startswith("requests/")], key=lambda item: item["parameters_sha256"])},
        "audit": {"acquired_at": receipt["audit"]["acquired_at"], "upstream_uri": receipt["upstream_uri"]}}
    parent_manifest = source_plan_contracts._source_manifest_from_plan(parent, "entity source")
    registry_object = planned("source_registry", "curated/source_registry.json", ["raw", "normalized"])
    # The compiler rereads every curated object through Git; proof/reader support
    # belongs to the derived source, not to this Git-native object selection.
    curation = {"source": CURATED_SOURCE, "source_kind": "curated_git_tree",
        "pin": {"type": "git_commit", "value": commit, "tree": tree},
        "objects": [{**registry_object, "root": GIT_ROOT, "path": REGISTRY_PATH}],
        "license": {"expression": "LicenseRef-WikiLean-Curated-Registry", "redistribution": "restricted",
                    "notice": "Committed source registry retained for private migration review."},
        "acquisition": {"name": "wikilean-curated-git-reader", "version": "1", "sha256": sha(canonical(git_tool))},
        "normalization": {"schema": "wikilean.curated-git-identity/v1", "tool": tool,
                          "inputs": ["source_registry"], "outputs": ["source_registry"]}}
    curated_manifest = source_plan_contracts._source_manifest_from_plan(curation, "registry source")
    derived_inputs = [{**parent_outputs[1], "roles": ["raw"]}, {**registry_object, "roles": ["raw"]}]
    derived_outputs = [planned("wikidata_crossrefs", "normalized/wikidata_crossrefs.json", ["normalized"]),
                       planned("requested_qid_scope", "normalized/requested_qid_scope.json", ["normalized"])]
    derived_lineage = lineage("wikidata-crossrefs", [{**object_ref(item), "origin": {"kind": "source_manifest", "id": origin}}
        for item, origin in zip(derived_inputs, (parent_manifest["source_manifest_id"], curated_manifest["source_manifest_id"]), strict=True)],
        derived_outputs, [parent_manifest["source_manifest_id"], curated_manifest["source_manifest_id"]], [],
        "evidence/crossref-lineage.json", NORMALIZATION_SCHEMA)
    git_support = [planned("git_reader", "curated/git-tool.json", ["receipt"])]
    git_support += [planned("git_proof_" + name, "curated/proof/" + name, ["receipt"], "application/octet-stream") for name in sorted(proof)]
    child = {"source": "wikidata-crossrefs", "source_kind": "sealed_snapshot",
        "pin": {"type": "dataset_revision", "value": derived_lineage["normalization_lineage_id"]},
        "objects": sorted([*derived_inputs, *derived_outputs, *support, *git_support], key=lambda item: item["name"]),
        "license": {"expression": "LicenseRef-Wikidata-Private-Audit", "redistribution": "restricted",
                    "notice": "Derived external IDs retain verified acquisition and registry ancestry; private source-plan fragment."},
        "acquisition": tool, "normalization": {"schema": NORMALIZATION_SCHEMA, "tool": tool,
            "inputs": sorted(item["name"] for item in derived_inputs), "outputs": sorted(item["name"] for item in derived_outputs)},
        "evidence": {"acquisition_receipts": [], "request_parameter_preimages": [],
            "normalization_lineage": evidence("evidence/crossref-lineage.json", normalization_lineage_id=derived_lineage["normalization_lineage_id"])}}
    child_manifest = source_plan_contracts._source_manifest_from_plan(child, "crossref source")
    contracts.validate_source_manifest_evidence_documents(parent_manifest, receipts={receipt["acquisition_receipt_id"]: receipt},
        lineage=parent_lineage, request_parameter_preimages={item["parameters_sha256"]: item for item in parent["evidence"]["request_parameter_preimages"]})
    contracts.validate_source_manifest_evidence_documents(child_manifest, receipts={}, lineage=derived_lineage,
        request_parameter_preimages={}, parent_source_manifests={m["source_manifest_id"]: m for m in (parent_manifest, curated_manifest)})
    for name, manifest in (("entities", parent_manifest), ("registry", curated_manifest), ("crossrefs", child_manifest)):
        files["source-manifests/" + name + ".json"] = canonical(manifest)
    fragment = {"schema": "wikilean.wikidata-crossref-source-plan-fragment/v1", "scope": "source-plan-fragment",
        "physical_root": PHYSICAL_ROOT, "git_root": GIT_ROOT, "source_publishable": False, "redistribution": "restricted",
        "sources": sorted([parent, curation, child], key=lambda item: item["source"]),
        "input_bindings": [{"input_id": "wikidata-crossrefs", "state": "present", "sources": ["wikidata-crossrefs"],
            "members": [{"path": "catalog/data/wikidata_crossrefs.json", "source": "wikidata-crossrefs", "object": "wikidata_crossrefs"}]}]}
    files["source-fragment.json"] = canonical(fragment)
    result = {"schema": EXPORT_SCHEMA, "entity_bundle_id": bundle["bundle_id"], "curated_commit": commit, "curated_tree": tree,
        "normalization_profile_id": profile["profile_id"], "normalized_at": when,
        "original_normalization_lineage_id": old_lineage["normalization_lineage_id"],
        "requested_qids": len(verified.requested_qids), "crossref_qids": len(normalized["xrefs"]),
        "source_manifest_ids": sorted(m["source_manifest_id"] for m in (parent_manifest, curated_manifest, child_manifest)),
        "files": {path: {"sha256": sha(data), "bytes": len(data)} for path, data in sorted(files.items())}}
    result["export_id"] = contracts.domain_hash(EXPORT_SCHEMA, result)
    files["export.json"] = canonical(result)
    return files
