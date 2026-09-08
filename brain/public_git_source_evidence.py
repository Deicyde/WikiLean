"""Fresh public Git captures and complete, independently replayable source trees.

The reviewed plan selects a public repository and immutable commit. GitHub commit
metadata identifies the tree; retained archive bytes must reconstruct that entire
tree, including executable modes and symlink blobs. Links are never extracted or
followed. These restricted source fragments grant no redistribution permission.
"""
from __future__ import annotations

import copy
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "brain"))
import mathlib_source_evidence as archive
import acquire_mathlib_sources as github

contracts = archive.contracts
PLAN_SCHEMA = "wikilean.public-git-source-plan/v1"
CAPTURE_SCHEMA = "wikilean.public-git-source-capture/v1"
EXPORT_SCHEMA = "wikilean.public-git-source-export/v1"
PROFILE_SCHEMA = "wikilean.public-git-source-profiles/v1"
TOOL_SCHEMA = "wikilean.public-git-source-tool/v1"
NORMALIZATION_SCHEMA = "wikilean.public-git-source-normalization/v1"
REGISTRY = ROOT / "brain/public_git_source_profiles.json"
TOOL_FILES = tuple(sorted({
    "brain/public_git_source_evidence.py", "brain/public_git_sources.py",
    "brain/mathlib_source_evidence.py", "brain/acquire_mathlib_sources.py",
    "brain/stage_io.py", "brain/tools/authority_contracts.py",
    "brain/tools/execution_environment.py", "brain/tools/source_plan_contracts.py",
}))
REPOSITORIES = {
    "formal-conjectures-source": "google-deepmind/formal-conjectures",
    "erdosproblems-source": "teorth/erdosproblems",
    "tauceti-source": "TauCetiProject/TauCeti",
}
PHYSICAL_ROOT = "public_git_export"
EvidenceError = archive.EvidenceError
canonical, sha, parse = archive.canonical, archive.sha, archive.parse
exact, read_regular = archive.exact, archive.read_regular


def require(condition, message):
    if not condition:
        raise EvidenceError(message)


def validate_plan(plan):
    exact(plan, {"schema", "source", "repository", "commit"}, "public Git plan")
    require(plan["schema"] == PLAN_SCHEMA and plan["source"] in REPOSITORIES and
            plan["repository"] == REPOSITORIES[plan["source"]], "unreviewed public repository")
    require(isinstance(plan["commit"], str) and archive.SHA1.fullmatch(plan["commit"]),
            "public Git source requires a full immutable commit")
    return plan


def request_specs(plan):
    validate_plan(plan)
    prefix = "/repos/" + plan["repository"]
    return [("source", "commit", prefix + "/git/commits/" + plan["commit"], "application/json", 4 * 1024 * 1024),
            ("source", "source_tar", prefix + "/tarball/" + plan["commit"], "application/gzip", archive.MAX_FILE)]


def profile_id(profile):
    return contracts.domain_hash("wikilean.public-git-source-tool-profile.v1", {"files": profile["files"]})


def profiles():
    raw = read_regular(REGISTRY, 1024 * 1024)
    value = exact(parse(raw, "profiles"), {"schema", "current_profile", "profiles"}, "profiles")
    require(raw == canonical(value) and value["schema"] == PROFILE_SCHEMA and isinstance(value["profiles"], list),
            "unsupported public Git profile registry")
    ids = []
    for profile in value["profiles"]:
        exact(profile, {"profile_id", "files"}, "profile")
        require(isinstance(profile["files"], list) and [item["path"] for item in profile["files"]] == list(TOOL_FILES),
                "public Git profile must cover the exact complete implementation")
        for item in profile["files"]:
            exact(item, {"path", "sha256"}, "profile member")
            contracts._digest(item["sha256"], "profile member digest")
        require(profile["profile_id"] == profile_id(profile), "profile identity differs")
        ids.append(profile["profile_id"])
    require(ids == sorted(set(ids)) and value["current_profile"] in ids, "invalid current profile")
    return value


def origins():
    archive.validate_module_origins()
    require(Path(archive.__file__).resolve() == ROOT / "brain/mathlib_source_evidence.py" and
            Path(github.__file__).resolve() == ROOT / "brain/acquire_mathlib_sources.py" and
            github.evidence is archive, "public Git acquisition helper origin differs")


def current_profile():
    origins()
    registry = profiles()
    profile = next(item for item in registry["profiles"] if item["profile_id"] == registry["current_profile"])
    require(profile["files"] == [{"path": name, "sha256": sha(read_regular(ROOT / name))} for name in TOOL_FILES],
            "current public Git implementation differs from its reviewed generation")
    return copy.deepcopy(profile)


def validate_tool(tool):
    exact(tool, {"schema", "profile_id", "files", "python", "gh"}, "acquisition tool")
    require(tool["schema"] == TOOL_SCHEMA, "unsupported public Git acquisition tool")
    profile = next((item for item in profiles()["profiles"] if item["profile_id"] == tool["profile_id"]), None)
    require(profile is not None and tool["files"] == profile["files"], "unreviewed acquisition implementation")
    for key in ("python", "gh"):
        exact(tool[key], {"sha256", "version"}, "tool executable")
        contracts._digest(tool[key]["sha256"], "tool executable digest")
    require(isinstance(tool["python"]["version"], str) and
            re.fullmatch(r"CPython 3\.12\.[0-9]+ -I -S", tool["python"]["version"]), "isolated CPython3.12 is required")
    require(isinstance(tool["gh"]["version"], str) and tool["gh"]["version"].startswith("gh version "), "invalid GitHub CLI version")
    return profile


def verify_programs(profile, programs):
    require(profile in profiles()["profiles"] and set(programs) == set(TOOL_FILES) and
            profile["files"] == [{"path": name, "sha256": sha(programs[name])} for name in TOOL_FILES],
            "retained program preimages do not match one reviewed generation")


def normalize(plan, raw):
    specs = request_specs(plan)
    require(set(raw) == {item[1] for item in specs} and all(0 < len(raw[item[1]]) <= item[4] for item in specs),
            "incomplete or oversized public Git acquisition")
    commit = parse(raw["commit"], "GitHub commit metadata", artifact=True)
    endpoint = "https://api.github.com/repos/" + plan["repository"] + "/git/commits/" + plan["commit"]
    require(isinstance(commit, dict) and commit.get("sha") == plan["commit"] and commit.get("url") == endpoint,
            "GitHub metadata does not identify the selected repository commit")
    tree = commit.get("tree")
    require(isinstance(tree, dict) and isinstance(tree.get("sha"), str) and archive.SHA1.fullmatch(tree["sha"]) and
            tree.get("url") == "https://api.github.com/repos/" + plan["repository"] + "/git/trees/" + tree["sha"],
            "commit metadata lacks its exact repository tree")
    return archive.extract_source(raw["source_tar"], tree["sha"]), tree["sha"]


def receipt(plan, raw, tool, when):
    validate_tool(tool)
    specs = request_specs(plan)
    requests = sorted(map(archive.request_descriptor, specs), key=canonical)
    value = {"schema": contracts.ACQUISITION_RECEIPT_SCHEMA_V1, "source": plan["source"],
        "upstream_uri": "https://github.com/" + plan["repository"], "pin": {"type": "git_commit", "value": plan["commit"]},
        "tool": {"name": "wikilean-public-git-acquirer", "version": "1", "sha256": sha(canonical(tool))},
        "requests": requests, "batch": {"status": "complete", "request_set_root": contracts.acquisition_request_set_root(requests),
            "requests_total": 2, "requests_succeeded": 2, "requests_failed": 0},
        "outputs": sorted((archive.ref(item[1], raw[item[1]], item[3]) for item in specs), key=lambda item: item["object"]),
        "audit": {"acquired_at": when}}
    value["acquisition_receipt_id"] = contracts.acquisition_receipt_identity(value)
    contracts.validate_acquisition_receipt(value)
    return value


def capture_files(plan, raw, tool, programs, when):
    profile = validate_tool(tool)
    verify_programs(profile, programs)
    normalize(plan, raw)
    files = {"plan.json": canonical(plan), "tool.json": canonical(tool), "receipt.json": canonical(receipt(plan, raw, tool, when)),
             **{"implementation/" + name: data for name, data in programs.items()}}
    observations = []
    for spec in request_specs(plan):
        name, data = spec[1], raw[spec[1]]
        files["raw/" + name] = data
        files["requests/" + name + ".json"] = canonical(archive.parameters(spec))
        observations.append({"object": name, "request": archive.request_descriptor(spec), "result": "gh-api-exit-zero",
                             "exit_code": 0, "sha256": sha(data), "bytes": len(data)})
    files["request-results.json"] = canonical(observations)
    return archive.manifest_files(files, CAPTURE_SCHEMA)


def verify_capture_files(files):
    plan = validate_plan(parse(files["plan.json"], "plan"))
    tool = parse(files["tool.json"], "tool")
    raw = {spec[1]: files["raw/" + spec[1]] for spec in request_specs(plan)}
    programs = {name: files["implementation/" + name] for name in TOOL_FILES}
    acquired = parse(files["receipt.json"], "receipt")["audit"]["acquired_at"]
    expected = capture_files(plan, raw, tool, programs, acquired)
    require(files == {name: data for name, data in expected.items() if name != "manifest.json"},
            "capture request, receipt, and output closure differs")
    return plan, raw, tool


def verify_capture(path):
    origins()
    files, _ = archive.read_bundle(path, CAPTURE_SCHEMA)
    return (*verify_capture_files(files), files)


def build_export(capture, profile, programs, when):
    plan, raw, tool = verify_capture_files(capture)
    verify_programs(profile, programs)
    tree_files, tree = normalize(plan, raw)
    files = {"acquisition/" + name: data for name, data in capture.items()}
    files.update({"implementation/" + name: data for name, data in programs.items()})
    files["normalization/profile.json"] = canonical(profile)
    files["normalization/plan.json"] = canonical(plan)
    normalizer = {"name": "wikilean-public-git-normalizer", "version": "1", "sha256": sha(canonical(profile))}
    # Unique physical-root names allow independent repository exports to coexist
    # in one source plan without rewriting retained source/evidence identities.
    physical_root = plan["source"].replace("-", "_") + "_export"
    def planned(name, data, roles, media="application/octet-stream"):
        path = "objects/sha256/" + sha(data)
        files.setdefault(path, data)
        return {"name": name, "root": physical_root, "path": path, "sha256": sha(data), "bytes": len(data),
                "media_type": media, "roles": sorted(roles), "redistribution": "restricted"}
    def ref(item):
        return {"object": item["name"], **{key: item[key] for key in ("sha256", "bytes", "media_type")}}
    index = {"repository": plan["repository"], "commit": plan["commit"], "tree": tree,
             "entries": [{"path": path, "mode": mode, "git_blob": archive.git_hash("blob", data),
                          "sha256": sha(data), "bytes": len(data)} for path, (mode, data) in sorted(tree_files.items())]}
    outputs = [planned("git_tree", canonical(index), ["normalized"], "application/json")]
    members = []
    for path, (mode, data) in sorted(tree_files.items()):
        name = "file-" + sha(path.encode())
        outputs.append(planned(name, data, ["normalized"]))
        if mode in {"100644", "100755"}:
            members.append({"path": path, "source": plan["source"], "object": name})
    original = parse(capture["receipt.json"], "receipt")
    raw_objects = [planned(spec[1], raw[spec[1]], ["raw"], spec[3]) for spec in request_specs(plan)]
    inputs = [{**ref(item), "origin": {"kind": "acquisition_receipt", "id": original["acquisition_receipt_id"]}} for item in raw_objects]
    lineage = {"schema": contracts.NORMALIZATION_LINEAGE_SCHEMA_V1, "source": plan["source"], "mode": "transform",
        "acquisition_receipt_ids": [original["acquisition_receipt_id"]], "parent_source_manifest_ids": [],
        "normalization_schema": NORMALIZATION_SCHEMA, "configuration_sha256": sha(canonical(plan)), "tool": normalizer,
        "inputs": sorted(inputs, key=lambda item: item["object"]), "outputs": sorted(map(ref, outputs), key=lambda item: item["object"]),
        "result": "complete", "audit": {"normalized_at": when}}
    lineage["normalization_lineage_id"] = contracts.normalization_lineage_identity(lineage)
    files["evidence/lineage.json"] = canonical(lineage)
    support = [planned("normalization_plan", canonical(plan), ["receipt"], "application/json"),
               planned("normalization_profile", canonical(profile), ["receipt"], "application/json"),
               planned("acquisition_tool", canonical(tool), ["receipt"], "application/json")]
    for prefix, retained in (("normalizer", programs), ("acquirer", {name: capture["implementation/" + name] for name in TOOL_FILES})):
        support.extend(planned(prefix + "_program_" + str(index), retained[name], ["receipt"], "text/x-python") for index, name in enumerate(TOOL_FILES))
    def evidence(path, identity):
        return {"root": physical_root, "path": path, "sha256": sha(files[path]), "bytes": len(files[path]), "media_type": "application/json", **identity}
    source = {"source": plan["source"], "source_kind": "acquired_dataset", "pin": original["pin"],
        "objects": sorted([*outputs, *raw_objects, *support], key=lambda item: item["name"]),
        "license": {"expression": "LicenseRef-Private-Upstream-Git", "redistribution": "restricted",
                    "notice": "Private complete Git source tree. Original license files are retained; source-plan and redistribution review remain required."},
        "acquisition": original["tool"], "normalization": {"schema": NORMALIZATION_SCHEMA, "tool": normalizer,
            "inputs": sorted(item["name"] for item in raw_objects), "outputs": sorted(item["name"] for item in outputs)},
        "evidence": {"acquisition_receipts": [evidence("acquisition/receipt.json", {"acquisition_receipt_id": original["acquisition_receipt_id"]})],
            "normalization_lineage": evidence("evidence/lineage.json", {"normalization_lineage_id": lineage["normalization_lineage_id"]}),
            "request_parameter_preimages": sorted([evidence("acquisition/requests/" + spec[1] + ".json", {
                "parameters_sha256": sha(canonical(archive.parameters(spec)))}) for spec in request_specs(plan)], key=lambda item: item["parameters_sha256"])}}
    manifest = archive.source_plan_contracts._source_manifest_from_plan(source, "public Git source")
    contracts.validate_source_manifest_evidence_documents(manifest, receipts={original["acquisition_receipt_id"]: original},
        lineage=lineage, request_parameter_preimages={item["parameters_sha256"]: {key: item[key] for key in ("parameters_sha256", "bytes", "media_type")}
            for item in source["evidence"]["request_parameter_preimages"]}, parent_source_manifests={})
    files["source-manifest.json"] = canonical(manifest)
    files["source-fragment.json"] = canonical({"schema": "wikilean.public-git-source-fragment/v1", "scope": "source-plan-fragment",
        "physical_root": physical_root, "source_publishable": False, "redistribution": "restricted", "sources": [source],
        "input_bindings": [{"input_id": plan["source"] + "-tree", "state": "present", "sources": [plan["source"]], "members": members}]})
    return archive.manifest_files(files, EXPORT_SCHEMA)


def verify_export(path):
    origins()
    files, manifest = archive.read_bundle(path, EXPORT_SCHEMA)
    capture = {name.removeprefix("acquisition/"): data for name, data in files.items() if name.startswith("acquisition/")}
    profile = parse(files["normalization/profile.json"], "normalization profile")
    programs = {name: files["implementation/" + name] for name in TOOL_FILES}
    lineage = parse(files["evidence/lineage.json"], "lineage")
    expected = build_export(capture, profile, programs, lineage["audit"]["normalized_at"])
    require(expected == {**files, "manifest.json": canonical(manifest)}, "export differs from independently reconstructed Git tree and lineage")
    source = contracts.validate_source_manifest(parse(files["source-manifest.json"], "source manifest"))
    contracts.verify_source_manifest_files(source, path)
    return {"source_manifest_id": source["source_manifest_id"], "source": source["source"], "export_id": manifest["identity"]}
