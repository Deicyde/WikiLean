"""Streaming evidence for the exact reviewed public Hugging Face datasets.

CSV normalization is byte identity. Metadata at the same immutable revision
must agree with the reviewed LFS pins; README bytes must match its Git blob.
Publisher license declarations are retained evidence, not permission to publish.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import sys
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.append(str(ROOT / "brain/tools"))
import authority_contracts as contracts
import source_plan_contracts
sys.path.append(str(ROOT / "brain"))
import stage_io

CAPTURE = "wikilean.huggingface-source-capture/v1"
EXPORT = "wikilean.huggingface-source-export/v1"
REGISTRY = ROOT / "brain/huggingface_source_profiles.json"
PINS = ROOT / "catalog/huggingface_pins.json"
PROFILE_SCHEMA = "wikilean.huggingface-source-profiles/v1"
TOOL_FILES = tuple(sorted(("brain/huggingface_source_evidence.py", "brain/huggingface_sources.py",
    "brain/stage_io.py", "brain/tools/authority_contracts.py", "brain/tools/execution_environment.py",
    "brain/tools/source_plan_contracts.py", "catalog/huggingface_pins.json")))
DATASETS = {"MathNetwork/MathlibGraph": "hf-mathnetwork-mathlibgraph",
            "uw-math-ai/math-graph": "hf-uw-math-graph",
            "uw-math-ai/theorem-matching": "hf-uw-theorem-matching"}
EXPECTED_FILES = {"MathNetwork/MathlibGraph": {"nodes.csv", "edges.csv"},
                  "uw-math-ai/math-graph": {"formal_dependency.csv", "slogan.csv", "statement_formal.csv"},
                  "uw-math-ai/theorem-matching": {"theorem_matching.csv"}}
BINDINGS = {"slogan.csv": ("slogan", "hf-uw-math-graph"),
            "statement_formal.csv": ("statement-formal", "hf-uw-math-graph"),
            "theorem_matching.csv": ("theorem-matching", "hf-uw-theorem-matching")}
LICENSES = {"apache-2.0": "Apache-2.0", "cc-by-4.0": "CC-BY-4.0", "cc-by-sa-4.0": "CC-BY-SA-4.0"}
ROOT_NAME = "huggingface_export"
SHA256 = re.compile(r"[0-9a-f]{64}\Z")
SHA1 = re.compile(r"[0-9a-f]{40}\Z")
MAX_CONTROL = 16 * 1024 * 1024


class EvidenceError(RuntimeError):
    pass


def origins():
    for module, relative in ((contracts, "brain/tools/authority_contracts.py"),
        (contracts.execution_environment_contract, "brain/tools/execution_environment.py"),
        (source_plan_contracts, "brain/tools/source_plan_contracts.py"), (stage_io, "brain/stage_io.py")):
        if Path(getattr(module, "__file__", "")).resolve(strict=True) != ROOT / relative:
            raise EvidenceError("unexpected helper module origin: " + relative)
    if source_plan_contracts.contracts is not contracts:
        raise EvidenceError("source-plan helper loaded a different authority module")


origins()


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def canonical(value):
    return contracts.canonical_json_bytes(value)


def parse(raw, label, *, artifact=False):
    return (contracts.parse_artifact_json_bytes if artifact else contracts.parse_json_bytes)(raw, location=label)


def exact(value, keys, label):
    if not isinstance(value, dict) or set(value) != set(keys):
        raise EvidenceError(label + ": unexpected fields")
    return value


def real_path(path):
    if not path.is_absolute() or ".." in path.parts or any(p.is_symlink() for p in (path, *path.parents)):
        raise EvidenceError("evidence paths require real absolute ancestry")


def file_ref(path, *, private=False):
    real_path(path)
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
            raise EvidenceError("evidence requires a regular single-link file")
        if private and (before.st_uid != os.getuid() or stat.S_IMODE(before.st_mode) != 0o644):
            raise EvidenceError("evidence file ownership or mode differs")
        digest = hashlib.sha256()
        count = 0
        while chunk := os.read(fd, 1024 * 1024):
            count += len(chunk)
            digest.update(chunk)
        signature = lambda s: (s.st_dev, s.st_ino, s.st_size, s.st_mtime_ns, s.st_ctime_ns)
        if signature(before) != signature(os.fstat(fd)) or signature(before) != signature(path.lstat()):
            raise EvidenceError("evidence changed while reading")
        return {"sha256": digest.hexdigest(), "bytes": count}
    finally:
        os.close(fd)


def control_bytes(path):
    descriptor = file_ref(path)
    if descriptor["bytes"] > MAX_CONTROL:
        raise EvidenceError("control document exceeds bound")
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        with os.fdopen(fd, "rb", closefd=False) as handle:
            raw = handle.read(MAX_CONTROL + 1)
    finally:
        os.close(fd)
    if len(raw) > MAX_CONTROL or sha(raw) != descriptor["sha256"] or len(raw) != descriptor["bytes"]:
        raise EvidenceError("control document changed")
    return raw


def read_control(path, *, artifact=False):
    raw = control_bytes(path)
    return parse(raw, str(path), artifact=artifact)


LOADED_HELPER_HASHES = {name: file_ref(ROOT / name)["sha256"] for name in TOOL_FILES
                       if name not in {"brain/huggingface_sources.py", "catalog/huggingface_pins.json"}}


def profile_id(profile):
    return contracts.domain_hash("wikilean.huggingface-source-tool-profile.v1", {"files": profile["files"]})


def profiles():
    registry = exact(read_control(REGISTRY), {"schema", "current_profile", "profiles"}, "registry")
    if control_bytes(REGISTRY) != canonical(registry) or registry["schema"] != PROFILE_SCHEMA or not isinstance(registry["profiles"], list):
        raise EvidenceError("unsupported reviewed registry")
    ids = []
    for profile in registry["profiles"]:
        exact(profile, {"profile_id", "files"}, "profile")
        names = []
        if not isinstance(profile["files"], list):
            raise EvidenceError("invalid reviewed helper closure")
        for item in profile["files"]:
            exact(item, {"path", "sha256"}, "helper")
            contracts.validate_literal_relative_path(item["path"], "helper path")
            if not isinstance(item["sha256"], str) or not SHA256.fullmatch(item["sha256"]):
                raise EvidenceError("invalid helper hash")
            names.append(item["path"])
        if names != sorted(set(names)) or "brain/huggingface_source_evidence.py" not in names \
                or profile["profile_id"] != profile_id(profile):
            raise EvidenceError("invalid reviewed whole generation")
        ids.append(profile["profile_id"])
    if ids != sorted(set(ids)) or registry["current_profile"] not in ids:
        raise EvidenceError("invalid current profile")
    return registry


def approved_profile(profile):
    if profile not in profiles()["profiles"]:
        raise EvidenceError("unreviewed whole helper generation")
    return profile


def current_profile():
    origins()
    registry = profiles()
    profile = next(p for p in registry["profiles"] if p["profile_id"] == registry["current_profile"])
    actual = [{"path": p, "sha256": file_ref(ROOT / p)["sha256"]} for p in TOOL_FILES]
    if actual != profile["files"]:
        raise EvidenceError("current implementation differs from reviewed whole generation")
    if any(next(item["sha256"] for item in actual if item["path"] == name) != digest
           for name, digest in LOADED_HELPER_HASHES.items()):
        raise EvidenceError("loaded helper implementation changed since import")
    return profile


def validate_pins(pins):
    exact(pins, {"schema", "datasets"}, "pins")
    if pins["schema"] != "wikilean.huggingface-pins/v1" or not isinstance(pins["datasets"], dict) \
            or set(pins["datasets"]) != set(DATASETS):
        raise EvidenceError("unexpected dataset selection")
    for dataset, row in pins["datasets"].items():
        exact(row, {"revision", "verified_at", "verification", "files"}, "dataset pins")
        if not isinstance(row["revision"], str) or not SHA1.fullmatch(row["revision"]) \
                or not isinstance(row["files"], dict) or set(row["files"]) != EXPECTED_FILES[dataset]:
            raise EvidenceError("invalid immutable revision or reviewed file set")
        for descriptor in row["files"].values():
            exact(descriptor, {"sha256", "size"}, "file pin")
            if not isinstance(descriptor["sha256"], str) or not SHA256.fullmatch(descriptor["sha256"]) \
                    or type(descriptor["size"]) is not int or descriptor["size"] <= 0:
                raise EvidenceError("invalid reviewed file bytes")
    return pins


def object_name(filename):
    return filename.replace(".", "_")


def specs(pins):
    validate_pins(pins)
    result = []
    for dataset, row in sorted(pins["datasets"].items()):
        base = f"https://huggingface.co/datasets/{dataset}/resolve/{row['revision']}/"
        common = {"dataset": dataset, "source": DATASETS[dataset]}
        result.append({**common, "object": "revision_metadata", "uri": f"https://huggingface.co/api/datasets/{dataset}/revision/{row['revision']}",
                       "query": [["blobs", "true"]], "media_type": "application/json", "limit": MAX_CONTROL})
        result.append({**common, "object": "dataset_readme", "uri": base + "README.md", "query": [],
                       "media_type": "text/markdown", "limit": MAX_CONTROL})
        for name, pin in sorted(row["files"].items()):
            result.append({**common, "object": object_name(name), "uri": base + name, "query": [],
                           "media_type": "text/csv", "limit": pin["size"]})
    return result


def parameters(spec):
    return {"method": "GET", "uri": spec["uri"], "query": spec["query"],
        "headers": {"Accept": "*/*", "User-Agent": "WikiLean-source-evidence/1"},
        "transport": {"client": "curl", "configuration": "disabled", "authentication": "none", "proxy": "disabled",
                      "protocols": ["https"], "redirect_protocols": ["https"], "max_redirects": 5,
                      "retry": 0, "connect_timeout_seconds": 30, "total_timeout_seconds": 3600,
                      "http_errors": "fail", "body_limit_bytes": spec["limit"], "cache": "no local cache or resume"}}


def request(spec):
    return {"kind": "http_get", "uri": spec["uri"], "parameters_sha256": sha(canonical(parameters(spec)))}


def raw_path(spec):
    return "raw/" + spec["source"] + "/" + spec["object"]


def request_path(spec):
    return "requests/" + spec["source"] + "/" + spec["object"] + ".json"


def validate_tool(tool):
    exact(tool, {"schema", "profile", "python", "curl"}, "acquisition tool")
    if tool["schema"] != "wikilean.huggingface-source-acquirer-tool/v1":
        raise EvidenceError("unsupported acquisition tool")
    approved_profile(tool["profile"])
    for name in ("python", "curl"):
        exact(tool[name], {"sha256", "version"}, name)
        if not isinstance(tool[name]["sha256"], str) or not SHA256.fullmatch(tool[name]["sha256"]):
            raise EvidenceError("invalid executable hash")
    if not isinstance(tool["python"]["version"], str) or not re.fullmatch(r"CPython 3\.12\.[0-9]+ -I -S", tool["python"]["version"]) \
            or not isinstance(tool["curl"]["version"], str) or not tool["curl"]["version"].startswith("curl "):
        raise EvidenceError("unexpected acquisition runtime")


def validate_metadata(pins, root, index):
    """Authenticate all selected bytes before issuing a complete receipt."""
    licenses = {}
    for dataset, row in pins["datasets"].items():
        prefix = root / "raw" / DATASETS[dataset]
        metadata = read_control(prefix / "revision_metadata", artifact=True)
        if not isinstance(metadata, dict) or metadata.get("id") != dataset or metadata.get("sha") != row["revision"] \
                or metadata.get("private") is not False or not isinstance(metadata.get("siblings"), list):
            raise EvidenceError("dataset metadata does not match the exact public revision")
        siblings = {}
        for item in metadata["siblings"]:
            if not isinstance(item, dict) or not isinstance(item.get("rfilename"), str) or item["rfilename"] in siblings:
                raise EvidenceError("duplicate or malformed metadata membership")
            siblings[item["rfilename"]] = item
        for filename, expected in row["files"].items():
            member = siblings.get(filename, {})
            actual = index[f"raw/{DATASETS[dataset]}/{object_name(filename)}"]
            lfs = member.get("lfs")
            if not isinstance(lfs, dict) or member.get("size") != expected["size"] or lfs.get("sha256") != expected["sha256"] \
                    or lfs.get("size") != expected["size"] \
                    or actual != {"sha256": expected["sha256"], "bytes": expected["size"]}:
                raise EvidenceError("CSV differs from reviewed pins or exact-revision LFS metadata")
        readme = control_bytes(prefix / "dataset_readme")
        member = siblings.get("README.md", {})
        git_blob = hashlib.sha1(b"blob " + str(len(readme)).encode() + b"\0" + readme).hexdigest()
        if member.get("blobId") != git_blob or member.get("size") != len(readme) or member.get("lfs") is not None:
            raise EvidenceError("README does not match the exact-revision Git blob")
        frontmatter = re.match(rb"\A---\r?\n(.*?)\r?\n---(?:\r?\n|\Z)", readme, re.DOTALL)
        declarations = re.findall(rb"(?m)^license: *([a-z0-9.-]+) *\r?$", frontmatter[1]) if frontmatter else []
        declared = declarations[0].decode() if len(declarations) == 1 else None
        card = metadata.get("cardData")
        metadata_license = card.get("license") if isinstance(card, dict) else None
        licenses[dataset] = LICENSES.get(declared, "LicenseRef-HuggingFace-Review") \
            if declared == metadata_license else "LicenseRef-HuggingFace-Review"
    return licenses


def receipt(pins, tool, index, dataset, when):
    selected = [s for s in specs(pins) if s["dataset"] == dataset]
    requests = sorted(map(request, selected), key=canonical)
    outputs = [{"object": s["object"], **index[raw_path(s)], "media_type": s["media_type"]} for s in selected]
    value = {"schema": contracts.ACQUISITION_RECEIPT_SCHEMA_V1, "acquisition_receipt_id": "sha256:" + "0" * 64,
        "source": DATASETS[dataset], "upstream_uri": "https://huggingface.co/datasets/" + dataset,
        "pin": {"type": "git_commit", "value": pins["datasets"][dataset]["revision"]},
        "tool": {"name": "wikilean-public-huggingface-acquirer", "version": "1", "sha256": sha(canonical(tool))},
        "requests": requests, "outputs": sorted(outputs, key=lambda x: x["object"]),
        "batch": {"status": "complete", "request_set_root": contracts.acquisition_request_set_root(requests),
                  "requests_total": len(selected), "requests_succeeded": len(selected), "requests_failed": 0},
        "audit": {"acquired_at": when}}
    value["acquisition_receipt_id"] = contracts.acquisition_receipt_identity(value)
    contracts.validate_acquisition_receipt(value)
    return value


def controls(pins, tool, index, when):
    validate_pins(pins)
    validate_tool(tool)
    result = {"pins.json": canonical(pins), "tool.json": canonical(tool)}
    records = []
    for spec in specs(pins):
        descriptor = index[raw_path(spec)]
        if descriptor["bytes"] > spec["limit"]:
            raise EvidenceError("response exceeds request bound")
        result[request_path(spec)] = canonical(parameters(spec))
        records.append({"source": spec["source"], "object": spec["object"], "request": request(spec),
                        "result": "curl-exit-zero; --fail enabled", "exit_code": 0, **descriptor})
    result["request-results.json"] = canonical(records)
    for dataset in sorted(DATASETS):
        result["receipts/" + DATASETS[dataset] + ".json"] = canonical(receipt(pins, tool, index, dataset, when))
    return result


def write(root, relative, raw):
    contracts.validate_literal_relative_path(relative, "bundle file")
    path = root / relative
    stage_io.ensure_private_directory(root, path.parent)
    stage_io.write_bytes_exclusive(path, raw, mode=0o644)


def copy_file(source, root, relative, expected):
    real_path(source)
    target = root / relative
    stage_io.ensure_private_directory(root, target.parent)
    inp = os.open(source, os.O_RDONLY | os.O_NOFOLLOW)
    out = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o644)
    try:
        before = os.fstat(inp)
        if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
            raise EvidenceError("copy requires a regular single-link source")
        os.fchmod(out, 0o644)
        digest, count = hashlib.sha256(), 0
        while chunk := os.read(inp, 1024 * 1024):
            count += len(chunk)
            if count > expected["bytes"]:
                raise EvidenceError("copy source grew")
            digest.update(chunk)
            view = memoryview(chunk)
            while view:
                view = view[os.write(out, view):]
        if {"sha256": digest.hexdigest(), "bytes": count} != expected:
            raise EvidenceError("copy source content differs")
        os.fsync(out)
    finally:
        os.close(inp)
        os.close(out)


def indexed(root):
    real_path(root)
    result, directories = {}, set()
    for current, names, files in os.walk(root, followlinks=False):
        directory = Path(current)
        real_path(directory)
        info = directory.lstat()
        if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700:
            raise EvidenceError("bundle directories must be private and owned")
        for name in names:
            child = directory / name
            if child.is_symlink() or not child.is_dir():
                raise EvidenceError("bundle directory substitution")
            directories.add(child.relative_to(root).as_posix())
        for name in files:
            relative = (directory / name).relative_to(root).as_posix()
            if relative == "manifest.json":
                continue
            contracts.validate_literal_relative_path(relative, "bundle member")
            result[relative] = file_ref(directory / name, private=True)
    expected = {p.as_posix() for name in result for p in Path(name).parents if p != Path(".")}
    if directories != expected:
        raise EvidenceError("bundle contains undeclared empty directories")
    return dict(sorted(result.items()))


def manifest(index, schema):
    value = {"schema": schema, "files": index}
    return {**value, "identity": contracts.domain_hash(schema, value)}


def verify_bundle(root, schema):
    index = indexed(root)
    file_ref(root / "manifest.json", private=True)
    value = read_control(root / "manifest.json")
    if value != manifest(index, schema) or control_bytes(root / "manifest.json") != canonical(value):
        raise EvidenceError("bundle content or manifest identity differs")
    return index


def seal(owned, store, schema):
    index = indexed(owned.path)
    value = manifest(index, schema)
    write(owned.path, "manifest.json", canonical(value))
    for current, _names, _files in os.walk(owned.path, topdown=False):
        stage_io.fsync_directory(Path(current))
    target = store / value["identity"].removeprefix("sha256:")
    try:
        stage_io.publish_directory_no_replace(owned, target)
    except FileExistsError:
        if verify_bundle(target, schema) != index:
            raise EvidenceError("existing immutable bundle differs")
    stage_io.fsync_directory(store)
    verify_bundle(target, schema)
    return target


def prepare_store(store):
    real_path(store)
    anchor = next((p for p in (store, *store.parents) if p.exists()), None)
    stage_io.ensure_private_directory(anchor, store)
    info = store.lstat()
    if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700:
        raise EvidenceError("output store must be private and owned")


def verify_capture(root):
    index = verify_bundle(root, CAPTURE)
    pins = validate_pins(read_control(root / "pins.json"))
    tool = read_control(root / "tool.json")
    validate_tool(tool)
    # The retained exact pin registry must be one of the reviewed acquirer bytes.
    recorded_pin = next((p["sha256"] for p in tool["profile"]["files"] if p["path"] == "catalog/huggingface_pins.json"), None)
    original_pins = root / "pins-preimage.json"
    if file_ref(original_pins)["sha256"] != recorded_pin or read_control(original_pins) != pins:
        raise EvidenceError("retained pin preimage differs from the reviewed acquisition profile")
    when = read_control(root / ("receipts/" + sorted(DATASETS.values())[0] + ".json"))["audit"]["acquired_at"]
    expected = controls(pins, tool, index, when)
    raw_names = {raw_path(s) for s in specs(pins)}
    if set(index) != raw_names | set(expected) | {"pins-preimage.json"}:
        raise EvidenceError("capture request/output closure differs")
    for name, raw in expected.items():
        if index[name] != {"sha256": sha(raw), "bytes": len(raw)}:
            raise EvidenceError("capture control bytes differ")
    licenses = validate_metadata(pins, root, index)
    return pins, tool, index, licenses


def export_documents(capture, pins, tool, index, licenses, profile, when):
    approved_profile(profile)
    normalizer = {"name": "wikilean-huggingface-identity-normalizer", "version": "1", "sha256": sha(canonical(profile))}
    files, copies, sources = {"normalization/tool-profile.json": canonical(profile)}, {}, []
    def planned(name, descriptor, roles, media, *, origin=None, raw=None):
        path = "objects/sha256/" + descriptor["sha256"]
        if raw is not None:
            files[path] = raw
        elif origin is not None:
            copies[path] = (origin, descriptor)
        return {"name": name, "root": ROOT_NAME, "path": path, "roles": roles, **descriptor,
                "media_type": media, "redistribution": "restricted"}
    def support(name, raw):
        return planned(name, {"sha256": sha(raw), "bytes": len(raw)}, ["receipt"], "application/json", raw=raw)
    def pointer(path, raw, **identity):
        files[path] = raw
        return {"root": ROOT_NAME, "path": path, "sha256": sha(raw), "bytes": len(raw), "media_type": "application/json", **identity}
    for dataset in sorted(DATASETS):
        source = DATASETS[dataset]
        receipt_raw = control_bytes(capture / f"receipts/{source}.json")
        receipt_doc = parse(receipt_raw, "receipt")
        selected = [s for s in specs(pins) if s["dataset"] == dataset]
        objects = [planned(s["object"], index[raw_path(s)], ["normalized", "raw"], s["media_type"], origin=capture / raw_path(s)) for s in selected]
        projections = sorted(({"object": o["name"], **{k: o[k] for k in ("sha256", "bytes", "media_type")}} for o in objects), key=lambda v: v["object"])
        lineage = {"schema": contracts.NORMALIZATION_LINEAGE_SCHEMA_V1, "normalization_lineage_id": "sha256:" + "0" * 64,
            "source": source, "mode": "identity", "acquisition_receipt_ids": [receipt_doc["acquisition_receipt_id"]], "parent_source_manifest_ids": [],
            "normalization_schema": "wikilean.huggingface-reviewed-byte-identity/v1", "configuration_sha256": sha(canonical(pins["datasets"][dataset])),
            "tool": normalizer, "inputs": [{**p, "origin": {"kind": "acquisition_receipt", "id": receipt_doc["acquisition_receipt_id"]}} for p in projections],
            "outputs": projections, "result": "complete", "audit": {"normalized_at": when}}
        lineage["normalization_lineage_id"] = contracts.normalization_lineage_identity(lineage)
        contracts.validate_normalization_lineage(lineage)
        evidence = {"acquisition_receipts": [pointer(f"evidence/{source}-receipt.json", receipt_raw, acquisition_receipt_id=receipt_doc["acquisition_receipt_id"])],
            "normalization_lineage": pointer(f"evidence/{source}-lineage.json", canonical(lineage), normalization_lineage_id=lineage["normalization_lineage_id"]),
            "request_parameter_preimages": sorted((pointer("evidence/requests/" + source + "/" + s["object"] + ".json", canonical(parameters(s)),
                parameters_sha256=sha(canonical(parameters(s)))) for s in selected), key=lambda r: r["parameters_sha256"])}
        objects += [support("acquisition_tool", canonical(tool)), support("normalization_tool_profile", canonical(profile)),
                    support("reviewed_pin_configuration", canonical(pins["datasets"][dataset])),
                    support("reviewed_pin_registry", control_bytes(capture / "pins-preimage.json"))]
        item = {"source": source, "source_kind": "acquired_dataset", "pin": receipt_doc["pin"],
            "objects": sorted(objects, key=lambda v: v["name"]), "acquisition": receipt_doc["tool"],
            "normalization": {"schema": lineage["normalization_schema"], "tool": normalizer,
                              "inputs": [p["object"] for p in projections], "outputs": [p["object"] for p in projections]},
            "license": {"expression": licenses[dataset], "redistribution": "restricted",
                "notice": "Publisher declaration from the sealed exact-revision README and metadata; source-plan and redistribution review required."},
            "evidence": evidence, "audit": receipt_doc["audit"] | {"upstream_uri": receipt_doc["upstream_uri"]}}
        source_manifest = source_plan_contracts._source_manifest_from_plan(item, "Hugging Face source")
        contracts.validate_source_manifest_evidence_documents(source_manifest, receipts={receipt_doc["acquisition_receipt_id"]: receipt_doc}, lineage=lineage,
            request_parameter_preimages={r["parameters_sha256"]: {k: r[k] for k in ("parameters_sha256", "bytes", "media_type")}
                                        for r in evidence["request_parameter_preimages"]})
        files[f"source-manifests/{source}.json"] = canonical(source_manifest)
        sources.append(item)
    fragment = {"schema": "wikilean.huggingface-source-plan-fragment/v1", "scope": "source-plan-fragment", "physical_root": ROOT_NAME,
        "source_publishable": False, "redistribution": "restricted", "review_state": "verified-exact-upstream-bytes; source-plan-review-required; private-export",
        "sources": sources, "input_bindings": sorted((
            {"input_id": binding, "state": "present", "sources": [source], "members": [
                {"path": "catalog/.cache/" + filename, "source": source, "object": object_name(filename)}]}
            for filename, (binding, source) in BINDINGS.items()), key=lambda v: v["input_id"])}
    files["source-fragment.json"] = canonical(fragment)
    return files, copies
