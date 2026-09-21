"""Evidence closure for fresh, restricted public-file captures.

The sole reviewed dispatch is the ProofWiki gzip XML dump. Normalization is
binary identity: gzip framing/integrity is checked, but XML and graph semantics
are deliberately outside this raw-source boundary.
"""
from __future__ import annotations

import copy
import gzip
import io
import re
import sys
import zlib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "brain"))
import mathlib_source_evidence as archive

contracts = archive.contracts
EvidenceError = archive.EvidenceError
canonical, sha, parse, exact = archive.canonical, archive.sha, archive.parse, archive.exact
read_regular = archive.read_regular
PLAN_SCHEMA = "wikilean.public-file-source-plan/v1"
CAPTURE_SCHEMA = "wikilean.public-file-source-capture/v1"
EXPORT_SCHEMA = "wikilean.public-file-source-export/v1"
PROFILE_SCHEMA = "wikilean.public-file-source-profiles/v1"
TOOL_SCHEMA = "wikilean.public-file-source-tool/v1"
NORMALIZATION_SCHEMA = "wikilean.public-file-identity/v1"
REGISTRY = ROOT / "brain/public_file_source_profiles.json"
TOOL_FILES = tuple(sorted({"brain/public_file_source_evidence.py", "brain/public_file_sources.py",
    "brain/mathlib_source_evidence.py", "brain/stage_io.py", "brain/tools/authority_contracts.py",
    "brain/tools/execution_environment.py", "brain/tools/source_plan_contracts.py"}))
SOURCE_POLICIES = {"proofwiki-dump": {"uri": "https://proofwiki.org/xmldump/latest.xml.gz",
    "minimum_bytes": 1024 * 1024, "maximum_bytes": 512 * 1024 * 1024,
    "maximum_uncompressed_bytes": 4 * 1024 * 1024 * 1024, "format": "gzip",
    "response_media_types": ["application/gzip", "application/octet-stream", "application/x-gzip"]}}
USER_AGENT = "WikiLean-source-evidence/1.0 (https://github.com/Deicyde/WikiLean; reference use only)"


def require(condition, message):
    if not condition:
        raise EvidenceError(message)


def origins():
    archive.validate_module_origins()
    require(Path(archive.__file__).resolve() == ROOT / "brain/mathlib_source_evidence.py", "public-file helper origin differs")


def profile_id(profile):
    return contracts.domain_hash("wikilean.public-file-source-profile.v1", {key: value for key, value in profile.items() if key != "profile_id"})


def profiles():
    raw = read_regular(REGISTRY, 1024 * 1024)
    value = exact(parse(raw, "profiles"), {"schema", "current_profile", "profiles"}, "profiles")
    require(raw == canonical(value) and value["schema"] == PROFILE_SCHEMA and isinstance(value["profiles"], list), "invalid profile registry")
    ids = []
    for profile in value["profiles"]:
        exact(profile, {"profile_id", "files", "source_policies"}, "profile")
        require(isinstance(profile["files"], list) and [item["path"] for item in profile["files"]] == list(TOOL_FILES), "profile lacks complete program closure")
        for item in profile["files"]:
            exact(item, {"path", "sha256"}, "program")
            contracts._digest(item["sha256"], "program digest")
        require(profile["source_policies"] == SOURCE_POLICIES, "profile contains an unreviewed source policy")
        require(profile["profile_id"] == profile_id(profile), "profile identity differs")
        ids.append(profile["profile_id"])
    require(ids == sorted(set(ids)) and value["current_profile"] in ids, "invalid current profile")
    return value


def current_profile():
    origins()
    registry = profiles()
    profile = next(item for item in registry["profiles"] if item["profile_id"] == registry["current_profile"])
    require(profile["files"] == [{"path": name, "sha256": sha(read_regular(ROOT / name))} for name in TOOL_FILES], "current public-file program differs from reviewed generation")
    return copy.deepcopy(profile)


def verify_programs(profile, programs):
    require(profile in profiles()["profiles"] and set(programs) == set(TOOL_FILES) and
        profile["files"] == [{"path": name, "sha256": sha(programs[name])} for name in TOOL_FILES], "program preimages do not match a reviewed whole generation")


def validate_plan(plan, profile):
    exact(plan, {"schema", "source", "uri"}, "public-file plan")
    require(plan["schema"] == PLAN_SCHEMA and plan["source"] in profile["source_policies"] and
        plan["uri"] == profile["source_policies"][plan["source"]]["uri"], "unsupported public-file source or endpoint")
    return profile["source_policies"][plan["source"]]


def validate_tool(tool):
    exact(tool, {"schema", "profile_id", "files", "python", "curl"}, "acquisition tool")
    require(tool["schema"] == TOOL_SCHEMA, "unsupported acquisition tool")
    profile = next((item for item in profiles()["profiles"] if item["profile_id"] == tool["profile_id"]), None)
    require(profile is not None and tool["files"] == profile["files"], "unreviewed acquisition tool generation")
    for name in ("python", "curl"):
        exact(tool[name], {"sha256", "version"}, "executable")
        contracts._digest(tool[name]["sha256"], "executable digest")
    require(isinstance(tool["python"]["version"], str) and re.fullmatch(r"CPython 3\.12\.[0-9]+ -I -S", tool["python"]["version"]), "isolated CPython3.12 is required")
    require(isinstance(tool["curl"]["version"], str) and tool["curl"]["version"].startswith("curl "), "invalid curl executable version")
    return profile


def parameters(plan, profile):
    policy = validate_plan(plan, profile)
    return {"method": "GET", "uri": plan["uri"], "headers": {"Accept": "application/gzip", "Accept-Encoding": "identity", "User-Agent": USER_AGENT},
        "transport": {"default_config": False, "credentials": False, "proxy": False, "tls_verification": True,
            "redirects": False, "retries": 0, "cache": False, "connect_timeout_seconds": 30,
            "timeout_seconds": 600, "maximum_response_bytes": policy["maximum_bytes"]}}


def request(plan, profile):
    return {"kind": "http_get", "uri": plan["uri"], "parameters_sha256": sha(canonical(parameters(plan, profile)))}


def normalize(plan, raw, response, profile):
    policy = validate_plan(plan, profile)
    exact(response, {"curl_exit_code", "http_status", "content_type", "sha256", "bytes"}, "response")
    require(type(response["curl_exit_code"]) is int and response["curl_exit_code"] == 0 and
            type(response["http_status"]) is int and response["http_status"] == 200, "public-file acquisition did not complete one successful HTTP GET")
    require(response["content_type"] in policy["response_media_types"] and response["sha256"] == sha(raw) and response["bytes"] == len(raw), "response metadata differs from the captured body")
    require(policy["minimum_bytes"] <= len(raw) <= policy["maximum_bytes"], "compressed source body violates reviewed size bounds")
    require(raw.startswith(b"\x1f\x8b\x08"), "source response is not a gzip body")
    count = 0
    try:
        with gzip.GzipFile(fileobj=io.BytesIO(raw), mode="rb") as handle:
            while chunk := handle.read(1024 * 1024):
                count += len(chunk)
                require(count <= policy["maximum_uncompressed_bytes"], "gzip source exceeds reviewed expansion bound")
    except (OSError, EOFError, zlib.error) as exc:
        raise EvidenceError("source gzip framing or checksum is invalid") from exc
    require(count > 0, "source gzip body is empty")
    return raw


def body_ref(raw):
    return {"object": "compressed_dump", "sha256": sha(raw), "bytes": len(raw), "media_type": "application/gzip"}


def capture_files(plan, raw, response, tool, programs, when):
    profile = validate_tool(tool)
    verify_programs(profile, programs)
    normalize(plan, raw, response, profile)
    request_doc = request(plan, profile)
    receipt = {"schema": contracts.ACQUISITION_RECEIPT_SCHEMA_V1, "source": plan["source"],
        "pin": {"type": "content_sha256", "value": sha(raw)}, "upstream_uri": plan["uri"],
        "tool": {"name": "wikilean-public-file-acquirer", "version": "1", "sha256": sha(canonical(tool))},
        "requests": [request_doc], "batch": {"status": "complete", "requests_total": 1, "requests_succeeded": 1, "requests_failed": 0,
            "request_set_root": contracts.acquisition_request_set_root([request_doc])},
        "outputs": [body_ref(raw)], "audit": {"acquired_at": when}}
    receipt["acquisition_receipt_id"] = contracts.acquisition_receipt_identity(receipt)
    contracts.validate_acquisition_receipt(receipt)
    return archive.manifest_files({"plan.json": canonical(plan), "tool.json": canonical(tool), "profile.json": canonical(profile),
        "request.json": canonical(parameters(plan, profile)), "response.json": canonical(response),
        "receipt.json": canonical(receipt), "raw/compressed_dump.gz": raw,
        **{"implementation/" + name: data for name, data in programs.items()}}, CAPTURE_SCHEMA)


def verify_capture_files(files):
    plan, tool, response = [parse(files[name + ".json"], name) for name in ("plan", "tool", "response")]
    profile = validate_tool(tool)
    require(files["profile.json"] == canonical(profile), "acquisition profile preimage differs")
    programs = {name: files["implementation/" + name] for name in TOOL_FILES}
    raw = files["raw/compressed_dump.gz"]
    expected = capture_files(plan, raw, response, tool, programs, parse(files["receipt.json"], "receipt")["audit"]["acquired_at"])
    require(files == {name: data for name, data in expected.items() if name != "manifest.json"}, "capture body, request, response or receipt closure differs")
    return plan, raw, response, tool


def verify_capture(path):
    origins()
    files, _ = archive.read_bundle(path, CAPTURE_SCHEMA)
    return (*verify_capture_files(files), files)


def build_export(capture, profile, programs, when):
    plan, raw, response, tool = verify_capture_files(capture)
    verify_programs(profile, programs)
    normalize(plan, raw, response, profile)
    files = {"acquisition/" + name: data for name, data in capture.items()}
    files.update({"implementation/" + name: data for name, data in programs.items()})
    files["normalization/profile.json"] = canonical(profile)
    physical_root = plan["source"].replace("-", "_") + "_export"
    def planned(name, data, roles, media="application/json"):
        path = "objects/sha256/" + sha(data); files.setdefault(path, data)
        return {"root": physical_root, "path": path, "name": name, "sha256": sha(data), "bytes": len(data),
                "media_type": media, "roles": sorted(roles), "redistribution": "restricted"}
    body = planned("compressed_dump", raw, ["raw", "normalized"], "application/gzip")
    receipt = parse(capture["receipt.json"], "receipt")
    normalizer = {"name": "wikilean-public-file-identity", "version": "1", "sha256": sha(canonical(profile))}
    lineage = {"schema": contracts.NORMALIZATION_LINEAGE_SCHEMA_V1, "source": plan["source"], "mode": "identity",
        "normalization_schema": NORMALIZATION_SCHEMA, "configuration_sha256": sha(canonical(plan)), "tool": normalizer,
        "acquisition_receipt_ids": [receipt["acquisition_receipt_id"]], "parent_source_manifest_ids": [],
        "inputs": [{**body_ref(raw), "origin": {"kind": "acquisition_receipt", "id": receipt["acquisition_receipt_id"]}}],
        "outputs": [body_ref(raw)], "result": "complete", "audit": {"normalized_at": when}}
    lineage["normalization_lineage_id"] = contracts.normalization_lineage_identity(lineage)
    files["evidence/lineage.json"] = canonical(lineage)
    support = [planned("normalization_profile", canonical(profile), ["receipt"]), planned("normalization_plan", canonical(plan), ["receipt"]),
        planned("acquisition_profile", capture["profile.json"], ["receipt"]), planned("acquisition_tool", canonical(tool), ["receipt"]),
        planned("response", canonical(response), ["receipt"])]
    for prefix, retained in (("normalizer", programs), ("acquirer", {name: capture["implementation/" + name] for name in TOOL_FILES})):
        support.extend(planned(prefix + "_program_" + str(index), retained[name], ["receipt"], "text/x-python") for index, name in enumerate(TOOL_FILES))
    def evidence_ref(path, **identity):
        return {"root": physical_root, "path": path, "sha256": sha(files[path]), "bytes": len(files[path]), "media_type": "application/json", **identity}
    source = {"source": plan["source"], "source_kind": "acquired_dataset", "pin": receipt["pin"],
        "objects": sorted([body, *support], key=lambda item: item["name"]),
        "license": {"expression": "LicenseRef-ProofWiki-Review", "redistribution": "restricted",
            "notice": "Private compressed ProofWiki dump for reference use only. Attribution, license and downstream content policies require separate review."},
        "acquisition": receipt["tool"], "normalization": {"schema": NORMALIZATION_SCHEMA, "tool": normalizer,
            "inputs": ["compressed_dump"], "outputs": ["compressed_dump"]},
        "evidence": {"acquisition_receipts": [evidence_ref("acquisition/receipt.json", acquisition_receipt_id=receipt["acquisition_receipt_id"])],
            "normalization_lineage": evidence_ref("evidence/lineage.json", normalization_lineage_id=lineage["normalization_lineage_id"]),
            "request_parameter_preimages": [evidence_ref("acquisition/request.json", parameters_sha256=sha(capture["request.json"]))]}}
    manifest = archive.source_plan_contracts._source_manifest_from_plan(source, "public-file source")
    contracts.validate_source_manifest_evidence_documents(manifest, receipts={receipt["acquisition_receipt_id"]: receipt}, lineage=lineage,
        request_parameter_preimages={sha(capture["request.json"]): {"parameters_sha256": sha(capture["request.json"]),
            "bytes": len(capture["request.json"]), "media_type": "application/json"}}, parent_source_manifests={})
    files["source-manifest.json"] = canonical(manifest)
    files["source-fragment.json"] = canonical({"schema": "wikilean.public-file-source-fragment/v1", "scope": "source-plan-fragment",
        "physical_root": physical_root, "source_publishable": False, "redistribution": "restricted", "sources": [source],
        "input_bindings": [{"input_id": "proofwiki-compressed-dump", "state": "present", "sources": [plan["source"]],
            "members": [{"path": "proofwiki/latest.xml.gz", "source": plan["source"], "object": "compressed_dump"}]}]})
    return archive.manifest_files(files, EXPORT_SCHEMA)


def verify_export(path):
    origins()
    files, manifest = archive.read_bundle(path, EXPORT_SCHEMA)
    capture = {name.removeprefix("acquisition/"): data for name, data in files.items() if name.startswith("acquisition/")}
    profile = parse(files["normalization/profile.json"], "normalization profile")
    programs = {name: files["implementation/" + name] for name in TOOL_FILES}
    when = parse(files["evidence/lineage.json"], "lineage")["audit"]["normalized_at"]
    expected = build_export(capture, profile, programs, when)
    require(expected == {**files, "manifest.json": canonical(manifest)}, "export differs from independent compressed-body identity replay")
    source = contracts.validate_source_manifest(parse(files["source-manifest.json"], "source manifest"))
    contracts.verify_source_manifest_files(source, path)
    return {"source": source["source"], "source_manifest_id": source["source_manifest_id"], "export_id": manifest["identity"]}
