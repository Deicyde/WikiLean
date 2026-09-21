"""Freeze a complete observed replay into a pack-bound release.

This module has no command-line interface for certifying existing output or a
caller-provided success receipt. The trusted reproducibility producer calls it
only immediately after its own successful OCI launcher subprocess. That producer
owns the full launch-record verification; this handoff independently re-verifies
the pack, context, sealed inputs/programs, and complete output while freezing.
The evidence is unsigned trusted-host evidence, not a remote attestation.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import authority_contracts as contracts
import build_release
import execution_environment as environment
import prepare_replay_v2 as preparation
import run_replay_v2 as runner

RELEASE_INPUTS = {
    "catalog/data/source_registry.json": "source-registry",
    "brain/data/community_edges.jsonl": "brain-community-edges",
}


def require(condition: bool, message: str) -> None:
    if not condition:
        raise contracts.VerificationError("offline release: " + message)


def verify_context(pack: dict, pack_root: Path, context) -> dict:
    """Bind every context declaration and member to verified pack authority."""
    replay = context.replay
    for actual, expected, label in (
        (replay.offline_pack_id, pack["offline_pack_id"], "pack"),
        (replay.source_set_root, pack["source_set_root"], "source set"),
        (replay.reducer_inventory_id, pack["inventory"]["inventory_id"], "inventory"),
        (replay.reducer_git_commit, pack["reducer"]["git_commit"], "reducer commit"),
        (replay.configuration_sha256, pack["configuration"]["sha256"], "configuration"),
        (replay.environment_sha256, pack["environment"]["sha256"], "environment"),
    ):
        require(actual == expected, "context differs from sealed " + label)
    require(runner.build_context.canonical_json_bytes(context.configuration.to_document())
            == contracts.verify_file_ref(pack_root, pack["configuration"], "configuration"),
            "context configuration differs from sealed canonical configuration bytes")
    inventory = contracts.parse_json_bytes(
        contracts.verify_file_ref(pack_root, pack["inventory"], "inventory"), location="inventory")
    require([stage.to_document() for stage in context.stages] == inventory["stages"],
            "context stage schedule differs from sealed inventory")
    bindings = {item["input_id"]: item for item in pack["input_bindings"]}
    declarations = {item["id"]: item for item in inventory["inputs"]}
    require({binding.input_id for binding in context.bindings} == set(bindings) == set(declarations),
            "context input declaration closure differs from pack")
    objects = {}
    for ref in pack["source_manifests"]:
        source = contracts.parse_json_bytes(contracts.verify_file_ref(pack_root, ref, "source"), location="source")
        for item in source["objects"]:
            objects[(source["source_manifest_id"], item["name"])] = (item, source["pin"])
    for binding in context.bindings:
        document = binding.to_document(include_physical=False)
        require(preparation._binding_projection(document) == bindings[binding.input_id],
                "context member selection differs from pack")
        declaration = declarations[binding.input_id]
        for key in ("cardinality", "class", "requirement", "root"):
            require(document[key] == declaration[key], "context input declaration differs from inventory")
        selector = "path" if declaration["cardinality"] == "one" else "path_pattern"
        require(document[selector] == declaration[selector], "context selector differs from inventory")
        for member in document["members"]:
            obj, pin = objects[(member["source_manifest_id"], member["object"])]
            require(all(member[key] == obj[key] for key in ("sha256", "bytes", "media_type"))
                    and member["pin"] == pin, "context member identity differs from sealed source")
    descriptor = contracts.parse_json_bytes(
        contracts.verify_file_ref(pack_root, pack["environment"], "environment"), location="environment")
    environment.validate_execution_environment(descriptor)
    require(descriptor["profile"] == environment.AUTHORITATIVE_OCI_PROFILE,
            "full offline release requires the pinned authoritative OCI profile")
    return descriptor


def freeze_replayed_output(*, manifest_path: Path, pack_root: Path, context_path: Path,
                           output_store: Path, launch_record: dict) -> dict[str, Any]:
    """Internal handoff after the producer's own fully verified successful launch."""
    manifest_bytes = environment.secure_file_digest(manifest_path)
    context_bytes = environment.secure_file_digest(context_path)
    pack, _raw = contracts.load_canonical_json(manifest_path)
    contracts.validate_offline_pack(pack)
    require(pack["schema"] == contracts.PACK_SCHEMA_V3, "a v3 evidence-bearing pack is required")
    contracts.verify_offline_pack_files(pack, pack_root, manifest_path=manifest_path)
    context = runner.build_context.BuildContext.load(context_path)
    descriptor = verify_context(pack, pack_root, context)
    require(launch_record.get("ok") is True and launch_record.get("profile") == "trusted-local-engine"
            and launch_record.get("offline_pack_id") == pack["offline_pack_id"]
            and launch_record.get("generation_id") == context.generation_id
            and launch_record.get("environment_id") == descriptor["environment_id"]
            and launch_record.get("runtime") == descriptor["runtime"],
            "producer launch handoff does not match the exact replay")
    reducer_files = tuple((ref["logical_path"], ref["bytes"], ref["sha256"])
                          for ref in pack["reducer"]["files"])
    stages = tuple(stage.id for stage in context.stages)

    def current_output():
        runner._verify_input_closure(context)
        runner._verify_code_closure(context, reducer_files)
        runner._verify_outputs(context, stages)
        runner._verify_scratch(context, stages)
        return runner._output_state(context, stages)

    before = current_output()
    output_paths = tuple(sorted(path for path, item in before.items() if item[-1] is not None))
    require(not set(output_paths) & set(RELEASE_INPUTS), "provenance inputs must not be reducer-owned outputs")
    sources = {}
    for relative, input_id in RELEASE_INPUTS.items():
        binding = context._binding(input_id)
        paths = context.members(input_id)
        require(binding.state == "present" and len(paths) == len(binding.members) == 1,
                input_id + " must be explicitly present, including an empty community-edge snapshot")
        member = binding.members[0]
        sources[relative] = (paths[0], member.sha256, member.byte_length)
    replay = context.replay
    binding = {"authority_root": replay.authority_root, "offline_pack_id": replay.offline_pack_id,
               "reducer_inventory_id": replay.reducer_inventory_id, "prior_state_root": replay.prior_state_root,
               "generation_id": context.generation_id}
    config = build_release.BuildConfig(
        repo_root=context.roots.output, output_store=output_store, semantic_epoch=replay.semantic_epoch,
        schedule="sealed-reducer-inventory", reducer_version="full-offline-replay/v1",
        authority_git_commit=replay.authority_git_commit, reducer_git_commit=replay.reducer_git_commit,
        configuration_sha256=replay.configuration_sha256, environment_sha256=replay.environment_sha256)

    def final_source_check():
        require(current_output() == before, "complete replay output or sealed inputs changed during release freezing")
        require(environment.secure_file_digest(manifest_path) == manifest_bytes
                and environment.secure_file_digest(context_path) == context_bytes,
                "pack manifest or context changed during release freezing")
        contracts.verify_offline_pack_files(pack, pack_root, manifest_path=manifest_path)

    result = build_release.build_release(config, _verified_replay=build_release._VerifiedReplayInputs(
        source_set_root=pack["source_set_root"], replay=binding, sources=sources,
        input_count=len(context.bindings), output_paths=output_paths), _before_publish=final_source_check)
    final_source_check()
    release, _raw = contracts.load_canonical_json(Path(result["manifest"]))
    contracts.verify_release_files(contracts.validate_release_manifest(release), Path(result["root"]))
    require({item["path"] for item in release["artifacts"]} == set(output_paths) | set(RELEASE_INPUTS),
            "frozen artifacts do not equal complete outputs and sealed provenance inputs")
    return result
