"""Closed normalization of three reviewed public Git source exports.

Only captured parent bytes supply source data. The exact legacy Lean parsers
and pure PyYAML SafeLoader supply normalization. Parent identities, selected
native Git paths, complete tool/dependency preimages, and lineage remain sealed.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "brain"))
import public_git_source_evidence as parent
import git_harvest_dependencies as dependencies
if "yaml" not in sys.modules:
    # Library/test startup can discover the installed package. The production
    # isolated CLI supplies an explicit root before reaching this import.
    yaml_spec = importlib.util.find_spec("yaml")
    dependencies.require(yaml_spec is not None and yaml_spec.origin is not None, "install pinned PyYAML6.0.3")
    dependencies.load_yaml(Path(yaml_spec.origin).parent)
import git_harvest_adapters as adapters

contracts = parent.contracts
canonical, sha, parse = parent.canonical, parent.sha, parent.parse
exact, require = parent.exact, parent.require
PLAN_SCHEMA = "wikilean.git-harvest-normalization-plan/v1"
EXPORT_SCHEMA = "wikilean.git-harvest-source-export/v1"
PROFILE_SCHEMA = "wikilean.git-harvest-normalizer-profiles/v1"
NORMALIZATION_SCHEMA = "wikilean.git-harvest-normalization/v1"
REGISTRY = ROOT / "brain/git_harvest_profiles.json"
PHYSICAL_ROOT = "git_harvest_export"
_LOADED_IMPLEMENTATION = None
FAMILIES = {
    "formal-conjectures": {"parent": "formal-conjectures-source", "scope": "FormalConjectures", "suffix": ".lean",
        "license_sha256": "cfc7749b96f63bd31c3c42b5c471bf756814053e847c10f3eb003417bc523d30"},
    "erdos": {"parent": "erdosproblems-source", "scope": "data/problems.yaml", "suffix": None,
        "license_sha256": "c71d239df91726fc519c6eb72d318ec65820627232b2f796219e87dcf35d0ab4"},
    "tauceti": {"parent": "tauceti-source", "scope": "TauCeti", "suffix": ".lean",
        "license_sha256": "b40930bbcf80744c86c46a12bc9da056641d722716c378f5659b9e555ef833e1"},
}
OUTPUTS = {"formal-conjectures": "catalog/data/formal_conjectures.jsonl", "erdos-joins": "catalog/data/erdos_joins.jsonl",
           "erdos-pages": "catalog/data/external/erdos_pages.jsonl", "erdos-links": "catalog/data/external/erdos_links.jsonl",
           "tauceti": "catalog/data/tauceti.jsonl"}
TOOL_FILES = tuple(sorted({*parent.TOOL_FILES, "brain/git_harvest_sources.py", "brain/export_git_harvest.py",
    "brain/git_harvest_adapters.py", "brain/git_harvest_dependencies.py", "brain/build_common.py", "brain/build_context.py",
    "brain/ingest/common.py", "brain/ingest/git_snapshot.py", "brain/ingest/formal_conjectures.py",
    "brain/ingest/erdosproblems.py", "brain/ingest/lean_repo.py"}))


def validate_plan(plan):
    exact(plan, {"schema", "reviewed_parent_manifest_ids", "families"}, "harvester plan")
    require(plan["schema"] == PLAN_SCHEMA and plan["families"] == FAMILIES, "unsupported harvester selection or license review")
    exact(plan["reviewed_parent_manifest_ids"], {value["parent"] for value in FAMILIES.values()}, "reviewed parents")
    for value in plan["reviewed_parent_manifest_ids"].values(): contracts._hash(value, "reviewed parent identity")
    return plan


def profile_id(profile):
    return contracts.domain_hash("wikilean.git-harvest-normalizer-profile.v1", {key: value for key, value in profile.items() if key != "profile_id"})


def profiles():
    raw = parent.read_regular(REGISTRY)
    registry = exact(parse(raw, "harvester profiles"), {"schema", "current_profile", "profiles"}, "profiles")
    require(registry["schema"] == PROFILE_SCHEMA and raw == canonical(registry) and isinstance(registry["profiles"], list),
            "unsupported harvester profile registry")
    ids = []
    for profile in registry["profiles"]:
        exact(profile, {"profile_id", "files", "runtime"}, "profile")
        require([item["path"] for item in profile["files"]] == list(TOOL_FILES), "harvester profile lacks the full local closure")
        for item in profile["files"]:
            exact(item, {"path", "sha256"}, "program digest")
            contracts._digest(item["sha256"], "program digest")
        require(profile["profile_id"] == profile_id(profile), "harvester profile identity differs")
        ids.append(profile["profile_id"])
    require(ids == sorted(set(ids)) and registry["current_profile"] in ids, "invalid current harvester profile")
    return registry


def module_origins():
    parent.origins()
    pairs = [(parent, "brain/public_git_source_evidence.py"), (adapters, "brain/git_harvest_adapters.py"),
             (dependencies, "brain/git_harvest_dependencies.py"), (adapters.common, "brain/ingest/common.py"),
             (adapters.fc, "brain/ingest/formal_conjectures.py"), (adapters.erdosproblems, "brain/ingest/erdosproblems.py"),
             (adapters.lean_repo, "brain/ingest/lean_repo.py"), (adapters.git_snapshot, "brain/ingest/git_snapshot.py"),
             (adapters.fc.build_common, "brain/build_common.py"), (sys.modules["build_context"], "brain/build_context.py")]
    for module, name in pairs:
        require(Path(module.__file__).resolve(strict=True) == ROOT / name, "harvester helper has a different origin: " + name)
    require(adapters.fc.common is adapters.common and adapters.lean_repo.fc is adapters.fc and
            adapters.lean_repo.build_common is adapters.fc.build_common and adapters.erdosproblems.common is adapters.common,
            "harvesters loaded inconsistent helper modules")


def current_implementation():
    module_origins()
    runtime, dependency_files = dependencies.capture()
    programs = {name: parent.read_regular(ROOT / name) for name in TOOL_FILES}
    profile = {"files": [{"path": name, "sha256": sha(programs[name])} for name in TOOL_FILES], "runtime": runtime}
    profile["profile_id"] = profile_id(profile)
    if _LOADED_IMPLEMENTATION is not None:
        require((profile, programs, dependency_files) == _LOADED_IMPLEMENTATION,
                "loaded normalizer generation changed on disk")
    return profile, programs, dependency_files


def current_profile():
    profile, programs, dependency_files = current_implementation()
    registry = profiles()
    require(profile in registry["profiles"] and profile["profile_id"] == registry["current_profile"],
            "actual normalizer or PyYAML runtime differs from its reviewed generation")
    return profile, programs, dependency_files


def validate_preimages(profile, programs, dependency_files):
    require(profile in profiles()["profiles"] and set(programs) == set(TOOL_FILES) and
            profile["files"] == [{"path": name, "sha256": sha(programs[name])} for name in TOOL_FILES],
            "normalizer preimages do not match one reviewed generation")
    require(profile["runtime"]["pyyaml"]["files"] == [{"path": name, "sha256": sha(raw), "bytes": len(raw)}
            for name, raw in sorted(dependency_files.items())], "PyYAML preimage closure differs")
    # Independent normalization uses the same measured interpreter and parser
    # behavior. A different host must supply an explicitly reviewed generation.
    require(dependencies.capture()[0] == profile["runtime"], "independent verifier runtime differs from the captured normalizer")


def capture_parents(plan, roots):
    validate_plan(plan)
    require(set(roots) == set(plan["reviewed_parent_manifest_ids"]), "exact three physical parent roots are required")
    captured = {}
    for name, path in sorted(roots.items()):
        # Read once, then reproduce the complete public-Git export using only
        # those captured bytes. No later loose read can substitute source data.
        files, manifest = parent.archive.read_bundle(path, parent.EXPORT_SCHEMA)
        acquisition = {key.removeprefix("acquisition/"): raw for key, raw in files.items() if key.startswith("acquisition/")}
        profile = parse(files["normalization/profile.json"], "parent profile")
        programs = {key: files["implementation/" + key] for key in parent.TOOL_FILES}
        lineage = parse(files["evidence/lineage.json"], "parent lineage")
        expected = parent.build_export(acquisition, profile, programs, lineage["audit"]["normalized_at"])
        require(expected == {**files, "manifest.json": canonical(manifest)}, "parent differs from independent public-Git verification")
        source_manifest = contracts.validate_source_manifest(parse(files["source-manifest.json"], "parent source"))
        require(source_manifest["source"] == name and source_manifest["source_manifest_id"] == plan["reviewed_parent_manifest_ids"][name],
                "parent differs from the explicitly reviewed source identity")
        fragment = parse(files["source-fragment.json"], "parent fragment")
        require(len(fragment["sources"]) == 1 and fragment["sources"][0]["source"] == name, "parent fragment source closure differs")
        upstream_plan, raw, _ = parent.verify_capture_files(acquisition)
        tree_files, tree = parent.normalize(upstream_plan, raw)
        captured[name] = {"files": files, "source": fragment["sources"][0], "manifest": source_manifest,
                          "tree_files": tree_files, "tree": tree, "commit": upstream_plan["commit"]}
    return captured


def reduce_parents(plan, captured):
    output, selections = {}, {}
    for family, options in plan["families"].items():
        item = captured[options["parent"]]
        license_mode, license_bytes = item["tree_files"].get("LICENSE", (None, b""))
        require(license_mode in {"100644", "100755"} and sha(license_bytes) == options["license_sha256"],
                "source license differs from the reviewed Apache2 artifact")
        snapshot = adapters.snapshot(item["commit"], item["tree"], item["tree_files"], options["scope"], options["suffix"])
        selections[family] = [entry.path for entry in snapshot.files]
        if family == "formal-conjectures": output[family] = adapters.formal_conjectures(snapshot)
        elif family == "tauceti": output[family] = adapters.tauceti(snapshot)
        else: output.update(adapters.erdos(snapshot))
    require(set(output) == set(OUTPUTS), "harvester output closure differs")
    return output, selections


def jsonl(meta, rows):
    return b"".join(contracts.canonical_artifact_json_bytes(row) + b"\n" for row in [{"_meta": meta}, *rows])


def object_ref(item):
    return {"object": item["name"], **{key: item[key] for key in ("sha256", "bytes", "media_type")}}


def build_documents(plan, captured, profile, programs, dependency_files, when):
    validate_plan(plan)
    validate_preimages(profile, programs, dependency_files)
    reduced, selections = reduce_parents(plan, captured)
    configuration = {"schema": NORMALIZATION_SCHEMA, "families": plan["families"],
        "reviewed_parent_manifest_ids": plan["reviewed_parent_manifest_ids"], "selected_native_paths": selections,
        "parsers": "existing Lean harvest_snapshot/harvest_rows; PyYAML pure SafeLoader",
        "external_pair": "deterministic complete pages/links with legacy ID normalization; clock-free metadata"}
    files = {"plan.json": canonical(plan), "normalization/profile.json": canonical(profile),
        "normalization/configuration.json": canonical(configuration), "normalization/audit.json": canonical({"normalized_at": when})}
    files.update({"implementation/" + path: raw for path, raw in programs.items()})
    files.update({"dependencies/" + path: raw for path, raw in dependency_files.items()})
    for name, item in sorted(captured.items()):
        # Preserve physical-root spelling and original fragment bytes. Physical
        # paths are not source identity and the three parent roots are distinct.
        for path in ("source-fragment.json", "source-manifest.json"):
            files["parents/" + name + "/" + path] = item["files"][path]

    def planned(name, path, roles, media="application/json"):
        raw = files[path]
        item = {"name": name, "root": PHYSICAL_ROOT, "path": path, "sha256": sha(raw), "bytes": len(raw),
                "media_type": media, "roles": sorted(roles), "redistribution": "restricted"}
        files.setdefault("objects/sha256/" + item["sha256"], raw)
        return item

    tool = {"name": "wikilean-git-harvest-normalizer", "version": "1", "sha256": sha(canonical(profile))}
    support = [planned("normalizer_profile", "normalization/profile.json", ["receipt"]),
               planned("normalizer_configuration", "normalization/configuration.json", ["receipt"])]
    support.extend(planned("normalizer_program_" + str(i), "implementation/" + path, ["receipt"], "text/x-python")
                   for i, path in enumerate(sorted(programs)))
    support.extend(planned("normalizer_dependency_" + str(i), "dependencies/" + path, ["receipt"], "application/octet-stream")
                   for i, path in enumerate(sorted(dependency_files)))
    children = []
    for family, options in sorted(plan["families"].items()):
        original = captured[options["parent"]]
        parent_id = original["manifest"]["source_manifest_id"]
        require(parent_id == plan["reviewed_parent_manifest_ids"][options["parent"]], "captured parent identity differs")
        originals = {item["name"]: item for item in original["source"]["objects"]}
        names = ["git_tree", *("file-" + sha(path.encode()) for path in ["LICENSE", *selections[family]])]
        raw_objects, inputs, outputs = [], [], []
        for name in sorted(names):
            prior = originals[name]
            require("normalized" in prior["roles"], "harvest input is not a normalized parent object")
            raw = original["files"][prior["path"]]
            require(sha(raw) == prior["sha256"] and len(raw) == prior["bytes"], "captured parent input differs")
            path = "inputs/sha256/" + prior["sha256"]
            files[path] = raw
            item = planned(name, path, ["raw"], prior["media_type"])
            raw_objects.append(item)
            inputs.append({**object_ref(item), "origin": {"kind": "source_manifest", "id": parent_id}})
        selected_outputs = sorted(name for name in OUTPUTS if name == family or (family == "erdos" and name.startswith("erdos-")))
        for name in selected_outputs:
            path = "normalized/" + OUTPUTS[name]
            files[path] = jsonl(*reduced[name])
            outputs.append(planned(name, path, ["normalized"], "application/x-ndjson"))
        source_name = "git-harvest-" + family
        lineage = {"schema": contracts.NORMALIZATION_LINEAGE_SCHEMA_V1, "source": source_name, "mode": "transform",
            "acquisition_receipt_ids": [], "parent_source_manifest_ids": [parent_id], "normalization_schema": NORMALIZATION_SCHEMA,
            "configuration_sha256": sha(files["normalization/configuration.json"]), "tool": tool,
            "inputs": sorted(inputs, key=lambda item: item["object"]),
            "outputs": sorted(map(object_ref, outputs), key=lambda item: item["object"]),
            "result": "complete", "audit": {"normalized_at": when}}
        lineage["normalization_lineage_id"] = contracts.normalization_lineage_identity(lineage)
        path = "evidence/" + family + ".json"
        files[path] = canonical(lineage)
        source = {"source": source_name, "source_kind": "sealed_snapshot",
            "pin": {"type": "dataset_revision", "value": lineage["normalization_lineage_id"]},
            "objects": sorted([*raw_objects, *outputs, *support], key=lambda item: item["name"]),
            "license": {"expression": "Apache-2.0", "redistribution": "restricted",
                "notice": "Reviewed exact upstream LICENSE retained; private normalization, no publication approval."},
            "acquisition": tool, "normalization": {"schema": NORMALIZATION_SCHEMA, "tool": tool,
                "inputs": sorted(item["name"] for item in raw_objects), "outputs": selected_outputs},
            "evidence": {"acquisition_receipts": [], "request_parameter_preimages": [], "normalization_lineage": {
                "root": PHYSICAL_ROOT, "path": path, "sha256": sha(files[path]), "bytes": len(files[path]),
                "media_type": "application/json", "normalization_lineage_id": lineage["normalization_lineage_id"]}}}
        manifest = parent.archive.source_plan_contracts._source_manifest_from_plan(source, "derived Git harvest")
        contracts.validate_source_manifest_evidence_documents(manifest, receipts={}, lineage=lineage,
            request_parameter_preimages={}, parent_source_manifests={parent_id: original["manifest"]})
        files["source-manifests/" + family + ".json"] = canonical(manifest)
        children.append(source)
    bindings = []
    for name, path in sorted(OUTPUTS.items()):
        family = "erdos" if name.startswith("erdos-") else name
        input_id = {"erdos-pages": "external-pages", "erdos-links": "external-links"}.get(name, name)
        source_name = "git-harvest-" + family
        bindings.append({"input_id": input_id, "state": "present", "sources": [source_name],
            "members": [{"path": Path(path).name if name in {"erdos-pages", "erdos-links"} else path,
                         "source": source_name, "object": name}]})
    files["source-fragment.json"] = canonical({"schema": "wikilean.git-harvest-fragment/v1", "scope": "source-plan-fragment",
        "physical_root": PHYSICAL_ROOT, "source_publishable": False, "redistribution": "restricted",
        "sources": sorted([*(item["source"] for item in captured.values()), *children], key=lambda source: source["source"]),
        "input_bindings": sorted(bindings, key=lambda binding: binding["input_id"])})
    return parent.archive.manifest_files(files, EXPORT_SCHEMA)


def verify_export(path, roots):
    current_implementation()
    files, manifest = parent.archive.read_bundle(path, EXPORT_SCHEMA)
    plan = validate_plan(parse(files["plan.json"], "harvester plan"))
    captured = capture_parents(plan, roots)
    profile = parse(files["normalization/profile.json"], "normalizer profile")
    programs = {name.removeprefix("implementation/"): raw for name, raw in files.items() if name.startswith("implementation/")}
    dependency_files = {name.removeprefix("dependencies/"): raw for name, raw in files.items() if name.startswith("dependencies/")}
    audit = exact(parse(files["normalization/audit.json"], "audit"), {"normalized_at"}, "audit")
    expected = build_documents(plan, captured, profile, programs, dependency_files, audit["normalized_at"])
    require(expected == {**files, "manifest.json": canonical(manifest)}, "harvest export differs from independent source reduction")
    ids = {}
    for family in sorted(FAMILIES):
        source_manifest = contracts.validate_source_manifest(parse(files["source-manifests/" + family + ".json"], "harvest source"))
        contracts.verify_source_manifest_files(source_manifest, path)
        ids[family] = source_manifest["source_manifest_id"]
    current_implementation()
    return {"export_id": manifest["identity"], "normalizer_profile_id": profile["profile_id"], "source_manifest_ids": ids}


_LOADED_IMPLEMENTATION = current_implementation()
