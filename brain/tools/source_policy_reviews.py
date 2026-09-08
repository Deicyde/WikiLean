"""Standalone, trusted-local policy reviews for private packs and public releases.

These contracts authenticate neither a reviewer nor a license grant. An operator
must independently choose the expected review ID. Validation checks exact bytes,
complete coverage and the recorded decision; semantic field-policy completeness
and the legal basis remain that operator's review. Existing source restrictions,
preflight results, authority acceptance and production gates are not modified.
"""
from __future__ import annotations

import copy
import hashlib
import os
import stat
from pathlib import Path

import authority_contracts as contracts

PRIVATE_SCHEMA = "wikilean.private-replay-policy-review/v1"
PUBLIC_SCHEMA = "wikilean.public-release-policy-review/v1"
EVIDENCE_DOMAIN = "policy-review-evidence/v1"
MAX_DOCUMENT_BYTES = 128 * 1024 * 1024
MAX_ATTACHMENT_BYTES = 32 * 1024 * 1024
MAX_SOURCES = 2048
MAX_OBJECTS = 1_000_000
MAX_ARTIFACTS = 200_000
MAX_EVIDENCE = 4096
SCOPE = {"trust": "expected-id-pinned-local-review", "reviewer_authenticated": False,
         "legal_determination": False, "accepted_authority": False,
         "production_activation": False}
STATES = {"pending", "approved", "rejected"}
CONTENT_KINDS = {"facts", "descriptive-metadata", "licensed-prose", "licensed-code", "generated-program-output"}


class PolicyReviewError(ValueError):
    pass


def require(value, message):
    if not value:
        raise PolicyReviewError(message)


def exact(value, keys, label):
    require(isinstance(value, dict) and set(value) == set(keys), label + " has unexpected or missing fields")
    return value


def text(value, label, *, empty=False, maximum=4096):
    require(isinstance(value, str) and (empty or bool(value.strip())) and len(value) <= maximum,
            label + " must be bounded text")
    return value


def strings(value, label, *, empty=True, maximum=4096):
    require(isinstance(value, list) and len(value) <= maximum and (empty or bool(value)), label + " must be a bounded list")
    for entry in value:
        text(entry, label + " entry", maximum=1024)
    require(value == sorted(set(value)), label + " must be unique and sorted")
    return value


def canonical(value):
    return contracts.canonical_json_bytes(value)


def same(left, right):
    return canonical(left) == canonical(right)


def identity(document):
    require(isinstance(document, dict) and document.get("schema") in {PRIVATE_SCHEMA, PUBLIC_SCHEMA}, "unknown review schema")
    return contracts.domain_hash(document["schema"], {k: v for k, v in document.items() if k != "review_id"})


def secure_read(path, *, limit=MAX_DOCUMENT_BYTES):
    """Bounded, stable regular-file read through pinned no-follow directories."""
    path = Path(path)
    require(path.is_absolute() and ".." not in path.parts, "review paths must be absolute without parent traversal")
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    descriptors = []
    try:
        descriptors.append(os.open(path.anchor, flags))
        for component in path.parts[1:-1]:
            descriptors.append(os.open(component, flags, dir_fd=descriptors[-1]))
        fd = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=descriptors[-1])
        try:
            before = os.fstat(fd)
            require(stat.S_ISREG(before.st_mode) and before.st_nlink == 1 and before.st_size <= limit,
                    "review/evidence file must be bounded, regular and single-link")
            chunks, count = [], 0
            while chunk := os.read(fd, min(1024 * 1024, limit + 1 - count)):
                chunks.append(chunk); count += len(chunk)
                require(count <= limit, "review/evidence file grew beyond its bound")
            signature = lambda s: (s.st_dev, s.st_ino, s.st_size, s.st_mtime_ns, s.st_ctime_ns, s.st_mode, s.st_nlink)
            require(signature(before) == signature(os.fstat(fd)) == signature(os.stat(path.name, dir_fd=descriptors[-1], follow_symlinks=False)),
                    "review/evidence file changed during reading")
            require(count == before.st_size, "review/evidence file length changed")
            return b"".join(chunks)
        finally:
            os.close(fd)
    finally:
        for fd in reversed(descriptors):
            os.close(fd)


def load_document(path):
    raw = secure_read(path)
    document = contracts.parse_json_bytes(raw, location="policy review control")
    require(raw == canonical(document), "policy review control must use canonical-json-v1")
    return document


def verified_pack(path):
    path = Path(path)
    raw = secure_read(path)
    pack = contracts.parse_json_bytes(raw, location="offline pack")
    require(raw == canonical(pack), "offline pack manifest must be canonical")
    contracts.validate_offline_pack(pack)
    require(pack["schema"] == contracts.PACK_SCHEMA_V3, "policy reviews require the evidence-bearing v3 pack")
    require(len(pack["source_manifests"]) <= MAX_SOURCES and len(pack["objects"]) <= MAX_OBJECTS, "pack exceeds policy review scope")
    contracts.verify_offline_pack_files(pack, path.parent, manifest_path=path)
    sources, objects = {}, {}
    for ref in pack["source_manifests"]:
        require(ref["bytes"] <= MAX_DOCUMENT_BYTES, "source manifest exceeds review bound")
        source = contracts.parse_json_bytes(contracts.verify_file_ref(path.parent, ref, "policy source"), location="policy source")
        source_id = source["source_manifest_id"]
        sources[source_id] = source
        for obj in source["objects"]:
            objects[(source_id, obj["name"])] = obj
            require(len(objects) <= MAX_OBJECTS, "source-role closure exceeds review bound")
    require(raw == secure_read(path), "pack changed during policy inspection")
    return pack, sources, objects


def object_records(objects):
    return [{"source_manifest_id": source_id, "object": name,
             **{key: obj[key] for key in ("sha256", "bytes", "media_type", "roles", "redistribution")}}
            for (source_id, name), obj in sorted(objects.items())]


def closure_root(objects):
    return contracts.domain_hash("policy-object-role-closure/v1", object_records(objects))


def pack_binding(pack, objects):
    return {"offline_pack_id": pack["offline_pack_id"], "source_set_root": pack["source_set_root"],
            "reducer_inventory_id": pack["inventory"]["inventory_id"],
            "object_role_root": closure_root(objects), "source_object_count": len(objects)}


def source_records(sources, objects):
    groups = {source_id: {} for source_id in sources}
    for key, obj in objects.items():
        groups[key[0]][key] = obj
    return {source_id: {"source_manifest_id": source_id, "source": source["source"],
            "source_kind": source["source_kind"], "pin": copy.deepcopy(source["pin"]),
            "original_license": copy.deepcopy(source["license"]),
            "object_role_root": closure_root(groups[source_id]), "source_object_count": len(groups[source_id])}
            for source_id, source in sorted(sources.items())}


def decision():
    return {"decision": "pending", "basis": "", "evidence_ids": [], "obligations": [],
            "unresolved": ["Review the exact source and its private-use conditions."]}


def draft_private(pack_path):
    pack, sources, objects = verified_pack(pack_path)
    result = {"schema": PRIVATE_SCHEMA, "review_id": "", "scope": {**SCOPE, "use": "private-acquisition-retention-replay-only"},
              "binding": pack_binding(pack, objects), "state": "pending", "reviewer": None, "evidence": [],
              "sources": [{**record, **decision()} for record in source_records(sources, objects).values()]}
    result["review_id"] = identity(result)
    return result


def evidence_id(record):
    return contracts.domain_hash(EVIDENCE_DOMAIN, {key: value for key, value in record.items() if key != "evidence_id"})


def evidence_records(document, objects, attachment_root):
    records = document["evidence"]
    require(isinstance(records, list) and len(records) <= MAX_EVIDENCE, "invalid policy evidence count")
    ids = []
    for record in records:
        require(isinstance(record, dict), "policy evidence must be an object")
        kind = record.get("kind")
        if kind == "pack-object":
            exact(record, {"evidence_id", "kind", "source_manifest_id", "object", "sha256", "bytes", "media_type"}, "pack evidence")
            key = (record["source_manifest_id"], record["object"])
            require(key in objects, "policy evidence names an absent pack object")
            require(same({k: record[k] for k in ("sha256", "bytes", "media_type")},
                         {k: objects[key][k] for k in ("sha256", "bytes", "media_type")}), "policy evidence differs from pack bytes")
        elif kind == "review-attachment":
            exact(record, {"evidence_id", "kind", "path", "sha256", "bytes", "media_type", "origin"}, "attachment evidence")
            contracts.validate_literal_relative_path(record["path"], "policy attachment")
            contracts._digest(record["sha256"], "attachment hash")
            contracts._expect_int(record["bytes"], "attachment bytes")
            contracts._expect_pattern(record["media_type"], "attachment media", contracts.MEDIA_TYPE_RE, "media type")
            text(record["origin"], "attachment origin", maximum=2048)
            require(record["bytes"] <= MAX_ATTACHMENT_BYTES and attachment_root is not None, "bounded attachment root is required")
            raw = secure_read(Path(attachment_root) / record["path"], limit=MAX_ATTACHMENT_BYTES)
            require(len(raw) == record["bytes"] and hashlib.sha256(raw).hexdigest() == record["sha256"], "policy attachment bytes differ")
        else:
            raise PolicyReviewError("unknown policy evidence kind")
        require(record["evidence_id"] == evidence_id(record), "policy evidence ID differs")
        ids.append(record["evidence_id"])
    require(ids == sorted(set(ids)), "policy evidence IDs must be unique and sorted")
    return set(ids)


def validate_envelope(document, schema, expected_id, use):
    require(document.get("schema") == schema, "wrong policy review schema")
    contracts._hash(expected_id, "independently expected policy review ID")
    require(document["review_id"] == expected_id == identity(document), "policy review differs from independently expected ID")
    require(same(document["scope"], {**SCOPE, "use": use}), "policy review exceeds its standalone scope")
    require(isinstance(document["state"], str) and document["state"] in STATES, "invalid review state")
    reviewer = document["reviewer"]
    if reviewer is None:
        require(document["state"] == "pending", "a completed review requires reviewer audit metadata")
    else:
        exact(reviewer, {"name", "role", "reviewed_at"}, "reviewer")
        text(reviewer["name"], "reviewer name", maximum=256)
        require(reviewer["role"] == "trusted-local-operator", "unsupported reviewer role")
        contracts._expect_pattern(reviewer["reviewed_at"], "review date", contracts.UTC_TIMESTAMP_RE, "UTC timestamp")


def validate_decision(record, evidence, used, *, artifact_paths=None):
    require(isinstance(record["decision"], str) and record["decision"] in STATES, "invalid scoped decision")
    text(record["basis"], "decision basis", empty=record["decision"] == "pending")
    refs = strings(record["evidence_ids"], "decision evidence")
    require(set(refs) <= evidence, "decision references unknown policy evidence")
    used.update(refs)
    strings(record["unresolved"], "unresolved conditions")
    require(isinstance(record["obligations"], list) and len(record["obligations"]) <= 128, "invalid obligation list")
    ids = []
    for obligation in record["obligations"]:
        exact(obligation, {"id", "description", "evidence_ids", "artifact_paths"}, "obligation")
        text(obligation["id"], "obligation ID", maximum=128)
        text(obligation["description"], "obligation description")
        refs = strings(obligation["evidence_ids"], "obligation evidence")
        require(set(refs) <= evidence, "obligation references absent evidence")
        used.update(refs)
        paths = strings(obligation["artifact_paths"], "obligation artifact paths")
        require(set(paths) <= (artifact_paths or set()), "obligation artifact is absent from this review")
        if record["decision"] == "approved":
            require(refs or paths, "approved obligation needs retained fulfillment evidence or an exact release artifact")
        ids.append(obligation["id"])
    require(ids == sorted(set(ids)), "obligations must be unique and sorted")
    if record["decision"] == "approved":
        require(record["evidence_ids"] and not record["unresolved"], "approved decision requires evidence and no unresolved condition")


def validate_private(document, pack_path, *, expected_id, attachment_root=None):
    exact(document, {"schema", "review_id", "scope", "binding", "state", "reviewer", "evidence", "sources"}, "private review")
    validate_envelope(document, PRIVATE_SCHEMA, expected_id, "private-acquisition-retention-replay-only")
    pack, sources, objects = verified_pack(pack_path)
    require(same(document["binding"], pack_binding(pack, objects)), "private review pack/object-role binding differs")
    evidence = evidence_records(document, objects, attachment_root)
    require(isinstance(document["sources"], list), "review sources must be a list")
    require(all(isinstance(item, dict) for item in document["sources"]), "source decisions must be objects")
    require([item.get("source_manifest_id") for item in document["sources"]] == sorted(sources), "private review must cover every source exactly once")
    used = set()
    expected_sources = source_records(sources, objects)
    for record in document["sources"]:
        base = expected_sources[record["source_manifest_id"]]
        exact(record, set(base) | set(decision()), "source decision")
        require(same({k: record[k] for k in base}, base), "source identity, original license or object-role coverage differs")
        validate_decision(record, evidence, used)
    require(used == evidence, "policy review carries unreferenced evidence")
    ready = document["state"] == "approved"
    if ready:
        require(all(item["decision"] == "approved" for item in document["sources"]), "approved private review has incomplete decisions")
    return {"review_id": expected_id, "offline_pack_id": pack["offline_pack_id"], "private_replay_policy_ready": ready,
            "scope": copy.deepcopy(SCOPE)}


def verified_release(path, pack):
    require(Path(path).name == "release.json", "reviewed release manifest must be the public release.json")
    raw = secure_read(path)
    release = contracts.parse_json_bytes(raw, location="release")
    require(raw == canonical(release), "release manifest must be canonical")
    contracts.validate_release_manifest(release)
    require(release["profile"] == contracts.OFFLINE_REPLAY_RELEASE_PROFILE, "standalone public policy supports only the offline replay release profile")
    require(len(release["artifacts"]) <= MAX_ARTIFACTS, "release exceeds policy artifact bound")
    require(release["replay"]["offline_pack_id"] == pack["offline_pack_id"]
            and release["source_set_root"] == pack["source_set_root"]
            and release["replay"]["reducer_inventory_id"] == pack["inventory"]["inventory_id"]
            and release["reducer"]["git_commit"] == pack["reducer"]["git_commit"]
            and release["reducer"]["configuration_sha256"] == pack["configuration"]["sha256"]
            and release["reducer"]["environment_sha256"] == pack["environment"]["sha256"], "release does not belong to the reviewed pack")
    contracts.verify_release_files(release, Path(path).parent)
    require(raw == secure_read(path), "release changed during policy inspection")
    return release


def byte_index(objects):
    indexed = {}
    for (source_id, name), obj in sorted(objects.items()):
        indexed.setdefault((obj["sha256"], obj["bytes"]), []).append({"source_manifest_id": source_id, "object": name})
    return indexed


def direct_objects(artifact, indexed):
    return indexed.get((artifact["sha256"], artifact["bytes"]), [])


def support_files(release):
    raw = canonical(release)
    return sorted([{"path": "release.json", "sha256": hashlib.sha256(raw).hexdigest(), "bytes": len(raw),
                    "media_type": "application/json", "role": "release-manifest"},
                   *[{**{k: item[k] for k in ("path", "sha256", "bytes")}, "media_type": "application/json",
                      "role": item["kind"] + "-attestation"} for item in release["attestations"]]], key=lambda item: item["path"])


def public_binding(pack, objects, release, private_id):
    return {**pack_binding(pack, objects), "private_review_id": private_id, "release_id": release["release_id"],
            "release_manifest_sha256": hashlib.sha256(canonical(release)).hexdigest(),
            "artifact_root": contracts.domain_hash("policy-release-artifact-closure/v1", release["artifacts"])}


def draft_public(pack_path, release_path, private_review, *, expected_private_id, attachment_root=None):
    validation = validate_private(private_review, pack_path, expected_id=expected_private_id, attachment_root=attachment_root)
    require(validation["private_replay_policy_ready"], "public draft requires an approved private replay review")
    pack, sources, objects = verified_pack(pack_path)
    release = verified_release(release_path, pack)
    result = {"schema": PUBLIC_SCHEMA, "review_id": "", "scope": {**SCOPE, "use": "exact-public-release-artifacts-only"},
              "binding": public_binding(pack, objects, release, expected_private_id), "state": "pending", "reviewer": None,
              "evidence": [], "sources": [], "artifacts": [], "support_files": [],
              "unexported_source_object_root": ""}
    for record in source_records(sources, objects).values():
        result["sources"].append({**record, **decision(), "public_role": "pending"})
    directly_exported = set()
    indexed = byte_index(objects)
    for artifact in release["artifacts"]:
        direct = direct_objects(artifact, indexed)
        directly_exported.update((item["source_manifest_id"], item["object"]) for item in direct)
        result["artifacts"].append({"artifact": copy.deepcopy(artifact), "direct_source_objects": direct,
            **decision(), "content_rules": [], "unresolved": ["Review every field and applicable attribution/notice obligation in these exact artifact bytes."]})
    result["support_files"] = [{"file": ref, **decision(), "publicly_visible_fields": [],
        "unresolved": ["Review all public manifest/attestation metadata for licensing and sensitive information."]} for ref in support_files(release)]
    result["unexported_source_object_root"] = closure_root({key: obj for key, obj in objects.items() if key not in directly_exported})
    result["review_id"] = identity(result)
    return result


def validate_public(document, pack_path, release_path, private_review, *, expected_id, expected_private_id, attachment_root=None, private_attachment_root=None):
    exact(document, {"schema", "review_id", "scope", "binding", "state", "reviewer", "evidence", "sources", "artifacts", "support_files", "unexported_source_object_root"}, "public review")
    validate_envelope(document, PUBLIC_SCHEMA, expected_id, "exact-public-release-artifacts-only")
    private = validate_private(private_review, pack_path, expected_id=expected_private_id, attachment_root=private_attachment_root)
    require(private["private_replay_policy_ready"], "public review requires an approved exact private review")
    pack, sources, objects = verified_pack(pack_path)
    release = verified_release(release_path, pack)
    require(same(document["binding"], public_binding(pack, objects, release, expected_private_id)), "public review identity/closure differs")
    evidence = evidence_records(document, objects, attachment_root)
    artifact_paths = {item["path"] for item in release["artifacts"]} | {item["path"] for item in support_files(release)}
    require(isinstance(document["sources"], list) and all(isinstance(item, dict) for item in document["sources"])
            and [s.get("source_manifest_id") for s in document["sources"]] == sorted(sources), "public review must classify every source exactly once")
    used, roles = set(), {}
    expected_sources = source_records(sources, objects)
    for record in document["sources"]:
        base = expected_sources[record["source_manifest_id"]]
        exact(record, set(base) | set(decision()) | {"public_role"}, "public source decision")
        require(same({k: record[k] for k in base}, base), "public source identity/role closure differs")
        require(isinstance(record["public_role"], str) and record["public_role"] in {"pending", "evidence-only", "content-parent"}, "unknown public source role")
        if record["decision"] == "approved":
            require(record["public_role"] != "pending", "approved public source needs an explicit role")
        roles[record["source_manifest_id"]] = record["public_role"]
        validate_decision(record, evidence, used, artifact_paths=artifact_paths)
    require(isinstance(document["artifacts"], list) and len(document["artifacts"]) == len(release["artifacts"]), "public review omits or adds artifacts")
    exported, mentioned = set(), set()
    indexed = byte_index(objects)
    for record, artifact in zip(document["artifacts"], release["artifacts"], strict=True):
        exact(record, {"artifact", "direct_source_objects", "content_rules"} | set(decision()), "artifact decision")
        require(same(record["artifact"], artifact), "review artifact identity/order differs")
        expected = direct_objects(artifact, indexed)
        require(same(record["direct_source_objects"], expected), "review omits direct source-object exports")
        exported.update((obj["source_manifest_id"], obj["object"]) for obj in expected)
        validate_decision(record, evidence, used, artifact_paths=artifact_paths)
        require(isinstance(record["content_rules"], list) and len(record["content_rules"]) <= MAX_SOURCES, "invalid artifact content rules")
        keys, rule_sources = [], set()
        for rule in record["content_rules"]:
            exact(rule, {"source_manifest_id", "content_kind", "fields", "license_expression", "interpretation"}, "content rule")
            source_id = rule["source_manifest_id"]
            require(source_id is None or isinstance(source_id, str) and source_id in sources, "content rule references absent source")
            require(isinstance(rule["content_kind"], str) and rule["content_kind"] in CONTENT_KINDS, "unknown content kind")
            require(source_id is not None or rule["content_kind"] == "generated-program-output", "only program-generated material can omit a source identity")
            if source_id is not None:
                require(roles[source_id] == "content-parent", "content rule contradicts evidence-only source role")
                mentioned.add(source_id); rule_sources.add(source_id)
            strings(rule["fields"], "reviewed artifact fields", empty=False, maximum=256)
            text(rule["license_expression"], "reviewed content license", maximum=1024)
            text(rule["interpretation"], "field-policy interpretation")
            keys.append(canonical(rule))
        require(keys == sorted(set(keys)), "content rules must be unique and canonically sorted")
        if record["decision"] == "approved":
            require(record["content_rules"], "approved artifact requires explicit content/field policy")
            require({obj["source_manifest_id"] for obj in expected} <= rule_sources, "directly exported source object has no content rule")
    support = support_files(release)
    require(isinstance(document["support_files"], list) and len(document["support_files"]) == len(support), "public review omits public support files")
    for record, ref in zip(document["support_files"], support, strict=True):
        exact(record, {"file", "publicly_visible_fields"} | set(decision()), "public support-file review")
        require(same(record["file"], ref), "public support-file identity differs")
        validate_decision(record, evidence, used, artifact_paths=artifact_paths)
        strings(record["publicly_visible_fields"], "public support fields", empty=record["decision"] != "approved", maximum=256)
    require(document["unexported_source_object_root"] == closure_root({key: obj for key, obj in objects.items() if key not in exported}), "private source-object remainder differs")
    require(used == evidence, "public review carries unreferenced evidence")
    ready = document["state"] == "approved"
    if ready:
        require(all(record["decision"] == "approved" for record in document["sources"] + document["artifacts"] + document["support_files"]), "approved public review has incomplete decisions")
        require(mentioned == {source_id for source_id, role in roles.items() if role == "content-parent"}, "public content-parent coverage differs")
    return {"review_id": expected_id, "private_review_id": expected_private_id,
            "offline_pack_id": pack["offline_pack_id"], "release_id": release["release_id"],
            "public_release_policy_ready": ready, "scope": copy.deepcopy(SCOPE)}
