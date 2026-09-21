#!/usr/bin/env python3
"""Run two actual trusted OCI replays, then verify their reproducibility evidence.

``run`` owns preparation and launch; it cannot import caller-supplied successful
replay summaries. The retained session is unsigned trusted-host evidence, like
the OCI launch record. Each successful launch is immediately frozen into a
fully verified pack-bound release. ``finalize`` requires the exact reviewed
session identity and re-verifies both produced releases before attesting.
Arbitrary same-user code or a compromised local engine is outside this boundary.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import random
import re
import sqlite3
import stat
import sys
from collections import Counter
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import authority_contracts as contracts
import build_replay_release
import execution_environment as environment
import launch_replay_oci as launcher
import oci_runtime
import prepare_replay_v2 as preparation
import run_replay_v2 as runner
import semantic_diff

APPROVAL_SCHEMA = "wikilean.reproducibility-approval/v1"
SESSION_SCHEMA = "wikilean.reproducibility-session/v1"
ATTESTATION_SCHEMA = "wikilean.reproducibility-attestation/v1"
DIAGNOSTIC_SCHEMA = "wikilean.reproducibility-diagnostic/v1"
BASE_PATHS = ("brain/data/nodes.jsonl", "brain/data/edges.jsonl", "brain/data/edges_links.jsonl")
SQLITE_PATH = "brain/data/brain.sqlite3"
RELEASE_INPUTS = {"catalog/data/source_registry.json": "source-registry",
                  "brain/data/community_edges.jsonl": "brain-community-edges"}
IDENTITY_POLICY = "brain-base-raw-header-without-snapshot-id/v1"
IMPLEMENTATION_PATHS = tuple(sorted({
    "brain/build_context.py", "brain/tools/reproducibility_gate.py",
    "brain/tools/authority_contracts.py", "brain/tools/execution_environment.py",
    "brain/tools/launch_replay_oci.py", "brain/tools/oci_runtime.py",
    "brain/tools/apparmor_runtime.py",
    "brain/tools/prepare_replay_v2.py", "brain/tools/run_replay_v2.py",
    "brain/tools/semantic_diff.py",
    "brain/tools/build_replay_release.py", "brain/tools/build_release.py",
}))
PROVENANCE_SECTIONS = {"edges", "organ_membership"}


class GateError(ValueError):
    """No authoritative reproducibility pass follows from this failure."""


def require(condition: bool, message: str) -> None:
    if not condition:
        raise GateError(message)


def canonical(value: Any) -> bytes:
    return contracts.canonical_json_bytes(value)


def sha(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def identity(domain: str, value: dict, field: str) -> str:
    return contracts.domain_hash(domain, {key: item for key, item in value.items() if key != field})


def exact(value: Any, fields: set[str], label: str) -> dict:
    require(isinstance(value, dict) and set(value) == fields, label + ": unexpected fields")
    return value


def read_document(path: Path) -> tuple[dict, bytes]:
    launcher._real_path(path, directory=False)
    value, raw = contracts.load_canonical_json(path)
    require(isinstance(value, dict), "expected an object: " + str(path))
    return value, raw


def write_new(path: Path, raw: bytes) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o400)
    with os.fdopen(descriptor, "wb") as stream:
        stream.write(raw)
        stream.flush()
        os.fsync(stream.fileno())
    preparation._fsync_directory(path.parent)


def implementation() -> list[dict]:
    root = HERE.parent.parent
    return [{"path": path, "sha256": environment.secure_file_digest(root / path)[0]}
            for path in IMPLEMENTATION_PATHS]


def validate_approval(value: dict) -> dict:
    exact(value, {"schema", "approval_id", "scope", "offline_pack_id", "source_set_root",
                  "reducer_inventory_id", "environment_id", "authority_git_commit", "authority_root",
                  "semantic_epoch", "prior_state_root", "baseline", "provenance"}, "approval")
    require(value["schema"] == APPROVAL_SCHEMA, "unsupported approval schema")
    require(value["scope"] in {"fixture", "full-corpus"}, "approval must explicitly name fixture or full-corpus scope")
    for key in ("offline_pack_id", "source_set_root", "reducer_inventory_id", "environment_id", "authority_root"):
        contracts._hash(value[key], key)
    if value["prior_state_root"] is not None:
        contracts._hash(value["prior_state_root"], "prior_state_root")
    contracts._expect_pattern(value["authority_git_commit"], "authority_git_commit", contracts.GIT_COMMIT_RE, "a full commit")
    contracts._expect_pattern(value["semantic_epoch"], "semantic_epoch", contracts.EPOCH_RE, "a semantic epoch")
    baseline = exact(value["baseline"], {"release_id", "manifest_sha256"}, "baseline approval")
    contracts._hash(baseline["release_id"], "baseline release_id")
    contracts._digest(baseline["manifest_sha256"], "baseline manifest_sha256")
    policy = exact(value["provenance"], {"mode", "expected_report_sha256"}, "provenance approval")
    require(policy["mode"] in {"exact", "reviewed-provenance-only"}, "unsupported provenance policy")
    if policy["mode"] == "exact":
        require(policy["expected_report_sha256"] is None, "exact policy cannot waive a migration report")
    else:
        contracts._digest(policy["expected_report_sha256"], "reviewed provenance report digest")
    require(value["approval_id"] == identity("wikilean.reproducibility-approval.v1", value, "approval_id"),
            "approval identity mismatch")
    return value


def capture_tree(root: Path) -> list[dict]:
    """Measure every file and directory, including empty directories and modes."""
    launcher._real_path(root, directory=True)
    require(root.stat().st_uid == os.getuid() and stat.S_IMODE(root.stat().st_mode) == 0o700,
            "output root must be current-user-owned mode 0700")
    entries = []
    for directory, names, filenames in os.walk(root, followlinks=False):
        parent = Path(directory)
        for name in sorted([*names, *filenames]):
            path = parent / name
            metadata = path.lstat()
            require(metadata.st_uid == os.getuid(), "output entry belongs to a different user")
            relative = path.relative_to(root).as_posix()
            contracts.validate_literal_relative_path(relative, "output path")
            mode = stat.S_IMODE(metadata.st_mode)
            if stat.S_ISDIR(metadata.st_mode):
                require(mode == 0o700, "output directory must retain mode 0700")
                entries.append({"path": relative, "kind": "directory", "mode": mode})
            else:
                require(stat.S_ISREG(metadata.st_mode) and metadata.st_nlink == 1,
                        "output must contain only private regular files and directories")
                require(mode == (0o444 if relative == SQLITE_PATH else 0o644), "unexpected output file mode")
                digest, size = environment.secure_file_digest(path)
                after = path.lstat()
                require((metadata.st_dev, metadata.st_ino, metadata.st_ctime_ns) ==
                        (after.st_dev, after.st_ino, after.st_ctime_ns), "output changed during measurement")
                entries.append({"path": relative, "kind": "file", "mode": mode, "sha256": digest, "bytes": size})
    return sorted(entries, key=lambda item: item["path"])


def base_snapshot_identity(root: Path) -> str:
    """Recompute the current reducer's exact pre-stamp three-file identity.

    Only the top-level _meta.snapshot_id field is removed. All row bytes,
    provenance, generation pins, ordering and remaining header fields survive.
    The compact writer format is checked before reconstructing that preimage.
    """
    digests = []
    declared = set()
    for relative in BASE_PATHS:
        with (root / relative).open("rb") as stream:
            header = stream.readline(16 * 1024 * 1024)
            value = json.loads(header, object_pairs_hook=semantic_diff._duplicate_free_object)
            require(isinstance(value, dict) and set(value) == {"_meta"} and isinstance(value["_meta"], dict),
                    "base artifact lacks an exact metadata header")
            encode = lambda item: (json.dumps(item, ensure_ascii=False, separators=(",", ":"), allow_nan=False) + "\n").encode()
            require(header == encode(value), "base metadata differs from the reviewed compact writer format")
            snapshot_id = value["_meta"].pop("snapshot_id", None)
            contracts._digest(snapshot_id, relative + " snapshot_id")
            declared.add(snapshot_id)
            digest = hashlib.sha256(encode(value))
            while chunk := stream.read(1024 * 1024):
                digest.update(chunk)
            digests.append(digest.hexdigest())
    expected = sha("".join(digests).encode("ascii"))
    require(declared == {expected}, "base snapshot_id does not bind the actual three-file preimage")
    return expected


def measure_output(root: Path, *, semantic_epoch: str, context=None) -> dict:
    if context is not None:
        require(root == context.roots.output, "output root differs from prepared context")
        stages = tuple(stage.id for stage in context.stages)
        runner._verify_outputs(context, stages)
        runner._verify_scratch(context, stages)
    before = capture_tree(root)
    file_entries = {item["path"]: item for item in before if item["kind"] == "file"}
    required = set(contracts.REQUIRED_RELEASE_PATHS) - set(RELEASE_INPUTS)
    require(required <= set(file_entries), "replay output lacks complete compatibility/static/SQLite artifacts")
    artifacts = {}
    for relative, item in file_entries.items():
        media_type, logical_format = contracts._release_artifact_contract(relative)
        with (root / relative).open("rb") as stream:
            logical_root = contracts._artifact_logical_root_handle(stream, logical_format, relative)
        artifacts[relative] = {"path": relative, "sha256": item["sha256"], "bytes": item["bytes"],
                               "logical_root": logical_root, "logical_format": logical_format, "media_type": media_type}
    snapshot_id = base_snapshot_identity(root)
    with contracts.open_verified_file(root, artifacts[SQLITE_PATH], "replay SQLite") as handle:
        # The two provenance inputs belong to sealed input/, never to a stage's
        # owned output/. Their full static projection is verified after the real
        # release producer copies those exact bound members into its release.
        static_complete = set(RELEASE_INPUTS) <= set(artifacts)
        contracts._verify_sqlite_projection(handle, root, artifacts, verify_static_closure=static_complete)
        connection = sqlite3.connect((Path("/dev/fd") / str(handle.fileno())).as_uri() + "?mode=ro&immutable=1", uri=True)
        try:
            base, projection = connection.execute("SELECT base_snapshot_id, projection_id FROM snapshot WHERE singleton=1").fetchone()
        finally:
            connection.close()
    require(base == snapshot_id, "SQLite base identity differs from recomputed JSONL preimage")
    semantic_root = contracts.compatibility_semantic_state_root(semantic_epoch, snapshot_id,
        {path: artifacts[path]["logical_root"] for path in contracts.COMPATIBILITY_SEMANTIC_PATHS})
    # Also validate frontier/base/cell generation coherence and every comparison family.
    snapshot = semantic_diff._resolve_snapshot(root / "brain/data")
    semantic_diff._validate_generation(snapshot)
    require(capture_tree(root) == before, "output changed while its identities were independently verified")
    return {"output_root": contracts.domain_hash("wikilean.replay-output-closure.v1", before),
            "base_identity_policy": IDENTITY_POLICY, "base_snapshot_id": snapshot_id,
            "projection_id": projection, "semantic_state_root": semantic_root,
            "static_projection": "verified" if static_complete else "requires-pack-bound-release",
            "entries": before, "artifacts": list(artifacts.values())}


def comparison_report(baseline: Path, candidate: Path) -> dict:
    report = semantic_diff.compare_paths(baseline, candidate)
    # Absolute paths and optional release labels identify observations, not semantics.
    report.pop("from")
    report.pop("to")
    def projection(path: Path) -> str:
        rows = Counter()
        with path.open("rb") as stream:
            for raw in stream:
                row = contracts.parse_artifact_json_bytes(raw, location="provenance projection")
                require(isinstance(row, dict), "provenance projection requires JSONL objects")
                if "_meta" in row:
                    require(set(row) == {"_meta"}, "metadata cannot hide a semantic row")
                    continue
                row.pop("provenance", None)
                rows[sha(contracts.canonical_artifact_json_bytes(row))] += 1
        return contracts.domain_hash("wikilean.edge-content-without-provenance.v1",
            [{"sha256": digest, "count": count} for digest, count in sorted(rows.items())])
    before, after = semantic_diff._resolve_snapshot(baseline), semantic_diff._resolve_snapshot(candidate)
    roots = {semantic_diff.RELEASE_PATHS[name]: {"from": projection(before.artifacts[name]), "to": projection(after.artifacts[name])}
             for name in ("edges", "edges_links") if name in before.artifacts and name in after.artifacts}
    return {"schema": "wikilean.reproducibility-compatibility/v1", "semantic": report,
            "provenance_projection": {"policy": "strip-only-edge-provenance/v1", "roots": roots}}


def report_bytes(report: dict) -> bytes:
    # Detailed semantic evidence contains original corpus text and numbers.
    return contracts.canonical_artifact_json_bytes(report)


def verify_compatibility(report: dict, policy: dict) -> dict:
    exact(report, {"schema", "semantic", "provenance_projection"}, "compatibility report")
    require(report["schema"] == "wikilean.reproducibility-compatibility/v1", "unsupported compatibility report")
    semantic = report["semantic"]
    summary = semantic_diff.summarize_report(semantic)
    require(semantic.get("summary") == summary and semantic.get("different") == semantic_diff.summary_has_differences(summary),
            "semantic comparison summary disagrees with detailed evidence")
    require(semantic.get("coverage", {}).get("complete") is True, "semantic baseline comparison lacks complete artifact coverage")
    changes = {(section, kind): count for section, values in summary.items() for kind, count in values.items() if count}
    def non_provenance(variants):
        rows = Counter()
        for item in variants:
            row = {key: value for key, value in item["row"].items() if key != "provenance"}
            rows[contracts.canonical_artifact_json_bytes(row)] += item["count"]
        return rows
    # semantic-diff/v2 intentionally labels same-source pin edits as "changed".
    # Independently prove those exact variants retain every non-provenance byte
    # and multiplicity before accepting an explicitly reviewed migration report.
    changed_edges_are_provenance = all(non_provenance(item["before"]) == non_provenance(item["after"])
                                       for item in semantic["edges"]["changed"])
    provenance_only = all((section in PROVENANCE_SECTIONS and kind == "provenance_only") or
                          (section == "edges" and kind == "changed" and changed_edges_are_provenance)
                          for section, kind in changes)
    if policy["mode"] == "exact":
        require(not changes, "approved baseline has graph/topology/content/provenance changes")
        require(all(item["from"] == item["to"] for item in semantic["semantic_artifacts"].values()),
                "exact semantic artifact roots differ even though the summarized JSON values agree")
    else:
        require(provenance_only, "reviewed provenance policy cannot waive graph/topology/content or snippet changes")
        projection = report["provenance_projection"]
        require(projection["policy"] == "strip-only-edge-provenance/v1" and set(projection["roots"]) ==
                {"brain/data/edges.jsonl", "brain/data/edges_links.jsonl"}, "incomplete provenance projection")
        require(all(item["from"] == item["to"] for item in projection["roots"].values()),
                "exact non-provenance edge content differs")
        require(all(item["from"] == item["to"] for path, item in semantic["semantic_artifacts"].items()
                    if path not in projection["roots"]), "non-edge semantic artifact roots differ")
        require(sha(report_bytes(report)) == policy["expected_report_sha256"], "provenance migration differs from the exact reviewed report")
    return {"mode": policy["mode"], "report_sha256": sha(report_bytes(report)),
            "graph_topology_content": "equal", "provenance": "equal" if not changes else "reviewed-only"}


def verified_baseline(path: Path, approval: dict) -> dict:
    manifest, raw = read_document(path)
    contracts.validate_release_manifest(manifest)
    require(manifest["release_id"] == approval["baseline"]["release_id"] and
            sha(raw) == approval["baseline"]["manifest_sha256"], "baseline differs from explicit approval")
    contracts.verify_release_files(manifest, path.parent)
    return manifest


def verified_pack(manifest_path: Path, root: Path, approval: dict) -> tuple[dict, dict]:
    manifest, _raw = read_document(manifest_path)
    pack = contracts.validate_offline_pack(manifest)
    require(pack["schema"] == contracts.PACK_SCHEMA_V3, "authoritative replay requires verified offline-pack/v3 evidence")
    contracts.verify_offline_pack_files(pack, root, manifest_path=manifest_path)
    require(pack["offline_pack_id"] == approval["offline_pack_id"] and pack["source_set_root"] == approval["source_set_root"] and
            pack["inventory"]["inventory_id"] == approval["reducer_inventory_id"], "pack differs from approved source/reducer scope")
    descriptor = contracts.parse_json_bytes(contracts.verify_file_ref(root, pack["environment"], "environment"), location="environment")
    environment.validate_execution_environment(descriptor)
    require(descriptor["environment_id"] == approval["environment_id"] and descriptor["profile"] == environment.AUTHORITATIVE_OCI_PROFILE,
            "approved pinned OCI execution environment is required")
    return pack, descriptor


def verify_context(context, pack: dict, approval: dict) -> None:
    expected = {"offline_pack_id": pack["offline_pack_id"], "source_set_root": pack["source_set_root"],
                "reducer_inventory_id": pack["inventory"]["inventory_id"], "reducer_git_commit": pack["reducer"]["git_commit"],
                "configuration_sha256": pack["configuration"]["sha256"], "environment_sha256": pack["environment"]["sha256"],
                **{key: approval[key] for key in ("authority_git_commit", "authority_root", "semantic_epoch", "prior_state_root")}}
    require(all(getattr(context.replay, key) == value for key, value in expected.items()), "prepared context differs from approved complete replay identity")
    runner._verify_input_closure(context)
    runner._verify_code_closure(context, tuple((item["logical_path"], item["bytes"], item["sha256"]) for item in pack["reducer"]["files"]))
    descriptor = runner._verify_execution_environment(context.roots.output.parent, context)
    require(descriptor["environment_id"] == approval["environment_id"], "prepared runtime differs from approval")
    for binding in RELEASE_INPUTS.values():
        require(len(context.member_records(binding)) == 1, "authoritative release requires explicitly present sealed provenance inputs")


def randomize_mtimes(workspace: Path, seed: int, *, apply: bool = True) -> dict:
    require(type(seed) is int and 0 <= seed < 2**53, "timestamp seed must be a portable nonnegative integer")
    randomizer = random.Random(seed)
    rows = []
    for path in sorted(workspace.rglob("*")):
        require(not path.is_symlink(), "prepared workspace contains a symlink")
        # Keep output/scratch fresh; perturb every sealed input, program and control file.
        relative = path.relative_to(workspace).as_posix()
        if relative.split("/")[0] in {"output", "scratch"}:
            continue
        timestamp = randomizer.randrange(946684800, 1893456000) * 10**9 + randomizer.randrange(10**9)
        if apply:
            os.utime(path, ns=(timestamp, timestamp), follow_symlinks=False)
        require(path.stat().st_mtime_ns == timestamp, "prepared input mtime differs from its recorded adversarial schedule")
        rows.append({"path": relative, "mtime_ns": str(timestamp)})
    return {"seed": seed, "schedule_sha256": sha(canonical(rows)), "entries": len(rows)}


def hostile_environment(private: Path, index: int, *, prepare: bool = True) -> dict[str, str]:
    temporary = private / "host-tmp"
    if prepare:
        temporary.mkdir(mode=0o700)
    return {"PATH": "/nonexistent", "LANG": "C", "LC_ALL": "C", "TMPDIR": str(temporary),
            "HOME": str(private / "unreadable-home"), "XDG_CACHE_HOME": str(private / "unreadable-cache"),
            "PYTHONPATH": "/unreadable/poison-" + str(index), "PYTHONHOME": "/unreadable/python-" + str(index),
            "PYTHONHASHSEED": str(101 + 796 * index), "TZ": "Pacific/Kiritimati" if index == 0 else "America/Adak",
            "BRAIN_DATA": "/unreadable/data-" + str(index), "BRAIN_MATHLIB_CHECKOUT": "/unreadable/mathlib-" + str(index),
            "BRAIN_BUILD_CONTEXT": "/unreadable/context-" + str(index), "BRAIN_CACHE": "/unreadable/cache-" + str(index)}


def verify_launch_record(record: dict, *, context, pack: dict, descriptor: dict,
                         image: oci_runtime.VerifiedImage, policy: dict, runtime: dict) -> None:
    expected = {"schema", "profile", "ok", "runtime", "config_digest", "environment_id", "offline_pack_id",
                "generation_id", "policy_sha256", "engine_id", "engine_version", "docker_sha256", "container_id",
                "create_observation_sha256", "exit_observation_sha256", "observations", "request_sha256", "stdout_sha256", "stderr_sha256"}
    expected_schema = launcher.LAUNCH_SCHEMA
    if "apparmor" in policy:
        expected.add("apparmor")
        expected_schema = launcher.LAUNCH_SCHEMA_V2
    exact(record, expected, "OCI launch record")
    require(record["schema"] == expected_schema and record["profile"] == "trusted-local-engine" and record["ok"] is True,
            "successful trusted local OCI launch evidence is required")
    if "apparmor" in policy:
        facts = exact(record["apparmor"], {"name", "mode", "binary_sha256", "kernel_abi", "kernel_profile_sha256",
            "text_sha256", "parser_sha256"}, "loaded AppArmor observation")
        require(facts["mode"] == "enforce" and all(facts[key] == policy["apparmor"][key]
                for key in ("name", "binary_sha256", "kernel_abi", "text_sha256", "parser_sha256")),
                "launch did not bind the exact enforced AppArmor policy")
        contracts._digest(facts["kernel_profile_sha256"], "observed kernel AppArmor profile digest")
    for key, actual in {"runtime": image.runtime(), "config_digest": image.config_digest, "environment_id": descriptor["environment_id"],
                        "offline_pack_id": pack["offline_pack_id"], "generation_id": context.generation_id,
                        "policy_sha256": image.policy_sha256, "engine_id": runtime["engine_id"],
                        "engine_version": runtime["engine_version"], "docker_sha256": runtime["docker_sha256"]}.items():
        require(record[key] == actual, "OCI launch record disagrees on " + key)
    cid = record["container_id"]
    require(isinstance(cid, str) and launcher.CID_RE.fullmatch(cid) is not None, "invalid observed container identity")
    observations = exact(record["observations"], {"created", "exited"}, "engine observations")
    for digest_field, stage in (("create_observation_sha256", "created"), ("exit_observation_sha256", "exited")):
        require(record[digest_field] == sha(canonical(observations[stage])), "engine observation digest mismatch")
    name = observations["created"].get("Name", "").removeprefix("/")
    require(re.fullmatch(r"wikilean-replay-[0-9a-f]{32}", name) is not None,
            "engine observation lacks launch nonce")
    uid, gid = map(int, observations["created"]["Config"]["User"].split(":"))
    memory = observations["created"]["HostConfig"]["Memory"]
    engine_image_id = observations["created"].get("Image")
    require(engine_image_id in {image.manifest_digest, image.config_digest}, "container selected an unverified engine image")
    require((uid, gid) == (os.getuid(), os.getgid()) and memory == runtime["memory_bytes"],
            "observed runtime ownership/resources differ from this launch policy")
    _command, child, mounts = launcher.create_arguments(image, context.roots.output.parent, Path(runtime["pack_root"]),
        policy, uid=uid, gid=gid, name=name, memory_bytes=memory, engine_image_id=engine_image_id)
    for status in ("created", "exited"):
        require(observations[status].get("Name") == "/" + name and
                (observations[status].get("Config", {}).get("Labels") or {}).get(launcher.LAUNCH_LABEL) == name,
                "container launch name/ownership label changed")
        launcher.verify_container(observations[status], image=image, child=child, mounts=mounts,
            uid=uid, gid=gid, memory_bytes=memory, status=status, cid=cid,
            apparmor_profile=policy.get("apparmor", {}).get("name"), engine_image_id=engine_image_id)
    nonce = name.removeprefix("wikilean-replay-")
    payload = {"schema": launcher.CHANNEL_SCHEMA, "nonce": nonce, "runtime": image.runtime(), "policy": policy,
        "runner_arguments": ["--manifest", runtime["manifest"], "--root", runtime["pack_root"],
            "--context", str(context.roots.output.parent / "build-context.json"), "--expected-generation-id", context.generation_id,
            "--python", policy["python"], "--stage-timeout-seconds", str(runtime["stage_timeout_seconds"])]}
    require(record["request_sha256"] == sha(canonical(payload)), "launch did not request the exact context/stages/pack")
    for key in ("stdout_sha256", "stderr_sha256"):
        contracts._digest(record[key], key)


def run_gate(args: argparse.Namespace) -> dict:
    runner.require_isolated_startup()
    require(sys.platform.startswith("linux"), "prerequisite missing: native Linux trusted OCI runtime; this host cannot issue replay evidence")
    approval, approval_raw = read_document(args.approval)
    validate_approval(approval)
    verified_baseline(args.baseline, approval)
    pack, descriptor = verified_pack(args.manifest, args.root, approval)
    policy, _raw = read_document(args.policy)
    image = oci_runtime.verify_image(args.oci_layout, descriptor["runtime"]["manifest_digest"], policy, args.wheelhouse, descriptor)
    destination = args.destination
    launcher._real_path(destination.parent, directory=True)
    require(not destination.exists() and not destination.is_symlink(), "gate destination must be fresh")
    for source in (args.root, args.baseline.parent, args.oci_layout, args.wheelhouse):
        require(not (destination == source or destination in source.parents or source in destination.parents), "gate store overlaps immutable input")
    destination.mkdir(mode=0o700)
    runtime = {key: str(getattr(args, key)) for key in ("manifest", "root", "oci_layout", "policy", "wheelhouse", "docker", "socket")}
    runtime["pack_root"] = runtime.pop("root")
    runtime.update({key: getattr(args, key) for key in ("docker_sha256", "engine_id", "engine_version", "stage_timeout_seconds", "timeout_seconds", "memory_bytes")})
    # Canonical control documents exclude floating-point numbers. Preserve the
    # launcher's exact float argument spelling as a bounded decimal string.
    for key in ("stage_timeout_seconds", "timeout_seconds"):
        runtime[key] = str(runtime[key])
    initial_implementation = implementation()
    write_new(destination / "approval.json", approval_raw)
    write_new(destination / "runtime.json", canonical(runtime))
    builds = []
    try:
        for index, seed in enumerate((args.seed, args.seed ^ 0x5DEECE66D)):
            private = destination / ("first" if index == 0 else "second-with-a-different-path-length")
            private.mkdir(mode=0o700)
            workspace = private / "workspace"
            prepared = preparation.prepare_replay_v2(args.manifest, workspace, pack_root=args.root,
                authority_git_commit=approval["authority_git_commit"], authority_root=approval["authority_root"],
                semantic_epoch=approval["semantic_epoch"], prior_state_root=approval["prior_state_root"],
                expected_pack_schema=contracts.PACK_SCHEMA_V3, expected_offline_pack_id=pack["offline_pack_id"])
            context = runner.build_context.BuildContext.load(prepared.context_path)
            build_replay_release.verify_context(pack, args.root, context)
            verify_context(context, pack, approval)
            schedule = randomize_mtimes(workspace, seed)
            inherited = hostile_environment(private, index)
            receipt_path = private / "launch.json"
            command = [str(Path(sys.executable).resolve()), "-I", "-B", str(HERE / "launch_replay_oci.py")]
            for key, value in runtime.items():
                option = "root" if key == "pack_root" else key.replace("_", "-")
                command.extend(["--" + option, str(value)])
            command.extend(["--context", str(prepared.context_path), "--receipt", str(receipt_path)])
            code, stdout, stderr = launcher._bounded_process(command, env=inherited, cwd=private,
                timeout=args.timeout_seconds + 300, limit=launcher.OUTPUT_LIMIT)
            write_new(private / "launcher.stdout", stdout)
            write_new(private / "launcher.stderr", stderr)
            require(code == 0, "trusted OCI launcher failed; retained actual launcher logs contain diagnostics")
            record, receipt_raw = read_document(receipt_path)
            require(stdout == receipt_raw + b"\n" and not stderr, "launcher output does not match its exact retained launch record")
            context = runner.build_context.BuildContext.load(prepared.context_path)
            verify_context(context, pack, approval)
            verify_launch_record(record, context=context, pack=pack, descriptor=descriptor, image=image, policy=policy, runtime=runtime)
            measured = measure_output(workspace / "output", semantic_epoch=approval["semantic_epoch"], context=context)
            write_new(private / "output.json", canonical(measured))
            frozen = build_replay_release.freeze_replayed_output(manifest_path=args.manifest, pack_root=args.root,
                context_path=prepared.context_path, output_store=private / "releases", launch_record=record)
            require(measure_output(workspace / "output", semantic_epoch=approval["semantic_epoch"], context=context) == measured,
                    "release freezing changed the complete owned replay output")
            release_path = Path(frozen["manifest"])
            release = verified_pack_release(release_path, approval=approval, pack=pack, context=context, measured=measured)
            comparison = comparison_report(args.baseline, workspace / "output/brain/data")
            write_new(private / "compatibility.json", report_bytes(comparison))
            compatibility = verify_compatibility(comparison, approval["provenance"])
            builds.append({"directory": private.name, "workspace": str(workspace), "generation_id": prepared.generation_id,
                           "container_id": record["container_id"], "launch_sha256": sha(receipt_raw), "measurement_sha256": sha(canonical(measured)),
                           "stdout_sha256": sha(stdout), "stderr_sha256": sha(stderr), "mtime_schedule": schedule,
                           "release": {"manifest": str(release_path), "release_id": release["release_id"],
                                       "manifest_sha256": environment.secure_file_digest(release_path)[0]},
                           "inherited_environment": inherited, "identities": {key: measured[key] for key in
                               ("output_root", "base_snapshot_id", "projection_id", "semantic_state_root")}, "compatibility": compatibility})
        require(builds[0]["container_id"] != builds[1]["container_id"], "two builds must have distinct observed container IDs")
        require(builds[0]["generation_id"] == builds[1]["generation_id"], "two prepared contexts have different logical identities")
        require(builds[0]["identities"] == builds[1]["identities"], "two complete replay outputs/identities differ")
        require(builds[0]["release"]["release_id"] == builds[1]["release"]["release_id"], "two pack-bound release identities differ")
        require((destination / builds[0]["directory"] / "output.json").read_bytes() ==
                (destination / builds[1]["directory"] / "output.json").read_bytes(), "full owned output closures differ")
        verified_pack(args.manifest, args.root, approval)
        verified_baseline(args.baseline, approval)
        require(implementation() == initial_implementation, "gate implementation changed during replay")
        session = {"schema": SESSION_SCHEMA, "session_id": "", "status": "pending-attestation", "scope": approval["scope"],
                   "approval_id": approval["approval_id"], "baseline_manifest": str(args.baseline),
                   "runtime_sha256": sha(canonical(runtime)), "implementation": initial_implementation, "builds": builds,
                   "remaining": ["explicit review of this exact session identity and final evidence re-verification"]}
        session["session_id"] = identity("wikilean.reproducibility-session.v1", session, "session_id")
        write_new(destination / "session.json", canonical(session))
        return session
    except BaseException as exc:
        write_new(destination / "failure.json", canonical({"schema": SESSION_SCHEMA, "status": "failed", "authoritative": False,
            "completed_builds": len(builds), "error": {"type": type(exc).__name__, "message": str(exc)}}))
        raise


def verified_pack_release(path: Path, *, approval: dict, pack: dict, context, measured: dict) -> dict:
    manifest, _raw = read_document(path)
    contracts.validate_release_manifest(manifest)
    # The shared verifier checks actual SQLite/static content and the exact
    # profile-specific build attestation; the gate also closes correspondence
    # to the owned live replay outputs and approved inputs below.
    contracts.verify_release_files(manifest, path.parent)
    require(manifest["source_set_root"] == pack["source_set_root"] and
            manifest["authority"]["git_commit"] == approval["authority_git_commit"] and
            manifest["authority"]["semantic_state_root"] == measured["semantic_state_root"] and
            manifest["semantic_epoch"] == approval["semantic_epoch"], "release authority disagrees with replay")
    for key, expected in {"git_commit": pack["reducer"]["git_commit"], "configuration_sha256": pack["configuration"]["sha256"],
                          "environment_sha256": pack["environment"]["sha256"]}.items():
        require(manifest["reducer"][key] == expected, "release reducer differs from sealed pack")
    raw_outputs = {item["path"]: item for item in measured["entries"] if item["kind"] == "file"}
    require({artifact["path"] for artifact in manifest["artifacts"]} == set(raw_outputs) | set(RELEASE_INPUTS),
            "release must contain the complete owned output file closure plus exactly two sealed provenance inputs")
    for artifact in manifest["artifacts"]:
        output = raw_outputs.get(artifact["path"])
        if output is None and artifact["path"] in RELEASE_INPUTS and context is not None:
            members = context.member_records(RELEASE_INPUTS[artifact["path"]])
            require(len(members) == 1, "release provenance input requires one explicitly present sealed member")
            member = members[0]
            output = {"sha256": member.sha256, "bytes": member.byte_length}
        require(output is not None and all(artifact[key] == output[key] for key in ("sha256", "bytes")),
                "release artifact does not correspond to exact owned replay bytes")
    builds = [ref for ref in manifest["attestations"] if ref["kind"] == "build"]
    require(len(builds) == 1, "release requires exactly one build attestation")
    build = contracts.parse_json_bytes(contracts.verify_file_ref(path.parent, builds[0], "build"), location="build")
    contracts.validate_build_attestation(build)
    require(build["schema"] == contracts.BUILD_ATTESTATION_SCHEMA_V2, "legacy compatibility freezer cannot satisfy real-pack reproducibility")
    expected_inputs = {"authority_root": approval["authority_root"], "source_set_root": pack["source_set_root"],
                       "offline_pack_id": pack["offline_pack_id"], "reducer_inventory_id": pack["inventory"]["inventory_id"],
                       "prior_state_root": approval["prior_state_root"]}
    require(build["inputs"] == expected_inputs, "build-v2 does not bind exact prepared context inputs")
    require(context.replay.offline_pack_id == pack["offline_pack_id"], "release context belongs to another pack")
    require(manifest.get("replay") == {key: expected_inputs[key] for key in
                ("authority_root", "offline_pack_id", "reducer_inventory_id", "prior_state_root")} |
                {"generation_id": context.generation_id}, "release identity does not bind the exact prepared replay generation")
    return manifest


def finalize(args: argparse.Namespace) -> dict:
    session, session_raw = read_document(args.session)
    require(session.get("schema") == SESSION_SCHEMA and session.get("status") == "pending-attestation", "a complete pending-attestation session is required")
    require(session.get("session_id") == args.expected_session_id == identity("wikilean.reproducibility-session.v1", session, "session_id"),
            "session differs from explicitly reviewed exact evidence generation")
    require(session["implementation"] == implementation(), "session was produced by a different gate implementation")
    root = args.session.parent
    approval, _ = read_document(root / "approval.json")
    validate_approval(approval)
    require(session["approval_id"] == approval["approval_id"] and session["scope"] == approval["scope"], "session approval mismatch")
    runtime, runtime_raw = read_document(root / "runtime.json")
    require(sha(runtime_raw) == session["runtime_sha256"], "session runtime changed")
    pack, descriptor = verified_pack(Path(runtime["manifest"]), Path(runtime["pack_root"]), approval)
    policy, _ = read_document(Path(runtime["policy"]))
    image = oci_runtime.verify_image(Path(runtime["oci_layout"]), descriptor["runtime"]["manifest_digest"], policy, Path(runtime["wheelhouse"]), descriptor)
    verified_baseline(Path(session["baseline_manifest"]), approval)
    require(isinstance(session["builds"], list) and len(session["builds"]) == 2, "exactly two observed replays are required")
    require(session["builds"][0]["directory"] != session["builds"][1]["directory"] and
            session["builds"][0]["mtime_schedule"]["seed"] != session["builds"][1]["mtime_schedule"]["seed"],
            "distinct paths and timestamp schedules are required")
    releases = []
    observed = []
    stability_checks = []
    for index, build in enumerate(session["builds"]):
        contracts.validate_literal_relative_path(build["directory"], "run directory")
        private = root / build["directory"]
        workspace = private / "workspace"
        require(str(workspace) == build["workspace"], "session workspace path changed")
        context = runner.build_context.BuildContext.load(workspace / "build-context.json")
        build_replay_release.verify_context(pack, Path(runtime["pack_root"]), context)
        verify_context(context, pack, approval)
        require(randomize_mtimes(workspace, build["mtime_schedule"]["seed"], apply=False) == build["mtime_schedule"],
                "timestamp challenge changed after replay")
        require(build["inherited_environment"] == hostile_environment(private, index, prepare=False),
                "inherited environment challenge differs from gate policy")
        record, launch_raw = read_document(private / "launch.json")
        require(sha(launch_raw) == build["launch_sha256"], "exact trusted launch record changed")
        for name, key in (("launcher.stdout", "stdout_sha256"), ("launcher.stderr", "stderr_sha256")):
            require(environment.secure_file_digest(private / name)[0] == build[key], "retained launcher output changed")
        require((private / "launcher.stdout").read_bytes() == launch_raw + b"\n" and not (private / "launcher.stderr").read_bytes(),
                "retained launcher result does not match its launch record")
        verify_launch_record(record, context=context, pack=pack, descriptor=descriptor, image=image, policy=policy, runtime=runtime)
        measured = measure_output(workspace / "output", semantic_epoch=approval["semantic_epoch"], context=context)
        require(sha(canonical(measured)) == build["measurement_sha256"], "owned replay outputs changed after launch")
        require(environment.secure_file_digest(private / "output.json")[0] == build["measurement_sha256"],
                "retained complete output measurement changed after review")
        report = comparison_report(Path(session["baseline_manifest"]), workspace / "output/brain/data")
        compatibility = verify_compatibility(report, approval["provenance"])
        require(compatibility == build["compatibility"], "baseline comparison changed after review")
        require(environment.secure_file_digest(private / "compatibility.json")[0] == compatibility["report_sha256"],
                "retained compatibility report changed after review")
        release_path = Path(build["release"]["manifest"])
        require(release_path.is_relative_to(private / "releases") and
                environment.secure_file_digest(release_path)[0] == build["release"]["manifest_sha256"],
                "release differs from the producer-owned observed generation")
        release = verified_pack_release(release_path, approval=approval, pack=pack, context=context, measured=measured)
        require(release["release_id"] == build["release"]["release_id"], "producer-owned release identity changed")
        releases.append(release["release_id"])
        stability_checks.append((workspace / "output", measured["entries"], release_path, release,
                                 build["release"]["manifest_sha256"]))
        observed.append({"generation_id": context.generation_id, "container_id": record["container_id"],
                         "launch_sha256": sha(launch_raw), "measurement_sha256": sha(canonical(measured)),
                         "release_id": release["release_id"], **{key: measured[key] for key in
                             ("output_root", "base_snapshot_id", "projection_id", "semantic_state_root")}})
    require(releases[0] == releases[1], "pack-bound release IDs differ")
    require(observed[0]["container_id"] != observed[1]["container_id"], "reused container evidence cannot prove two builds")
    for key in ("generation_id", "measurement_sha256", "release_id", "output_root", "base_snapshot_id", "projection_id", "semantic_state_root"):
        require(observed[0][key] == observed[1][key], "verified builds disagree on " + key)
    # The first build must remain intact while the second build and baseline
    # are checked. This also detects accidental concurrent workspace writes.
    for output_root, entries, release_path, release, manifest_sha256 in stability_checks:
        require(capture_tree(output_root) == entries, "owned replay output changed during final verification")
        require(environment.secure_file_digest(release_path)[0] == manifest_sha256,
                "produced release manifest changed during final verification")
        contracts.verify_release_files(release, release_path.parent)
    verified_pack(Path(runtime["manifest"]), Path(runtime["pack_root"]), approval)
    verified_baseline(Path(session["baseline_manifest"]), approval)
    require(environment.secure_file_digest(args.session)[0] == sha(session_raw) and
            session["implementation"] == implementation(), "reviewed session or implementation changed during final verification")
    attestation = {"schema": ATTESTATION_SCHEMA, "attestation_id": "", "result": "pass", "scope": approval["scope"],
                   "authority": "full-corpus" if approval["scope"] == "full-corpus" else "diagnostic-fixture",
                   "session_id": session["session_id"], "approval_id": approval["approval_id"],
                   "offline_pack_id": pack["offline_pack_id"], "source_set_root": pack["source_set_root"],
                   "environment_id": descriptor["environment_id"], "baseline": approval["baseline"],
                   "compatibility": session["builds"][0]["compatibility"], "builds": observed}
    attestation["attestation_id"] = identity("wikilean.reproducibility-attestation.v1", attestation, "attestation_id")
    validate_attestation(attestation)
    launcher._real_path(args.output.parent, directory=True)
    require(args.output.parent.stat().st_uid == os.getuid() and not stat.S_IMODE(args.output.parent.stat().st_mode) & 0o077,
            "attestation output parent must be private to the current user")
    write_new(args.output, canonical(attestation))
    return attestation


def validate_attestation(value: dict) -> dict:
    """Validate a result document; artifact/launch verification is still mandatory."""
    exact(value, {"schema", "attestation_id", "result", "scope", "authority", "session_id", "approval_id",
                  "offline_pack_id", "source_set_root", "environment_id", "baseline", "compatibility", "builds"}, "attestation")
    require(value["schema"] == ATTESTATION_SCHEMA and value["result"] in {"pass", "fail"}, "invalid reproducibility result")
    require(value["scope"] in {"fixture", "full-corpus"}, "invalid reproducibility scope")
    expected_authority = "full-corpus" if value["scope"] == "full-corpus" and value["result"] == "pass" else "diagnostic-fixture" if value["scope"] == "fixture" else "none"
    require(value["authority"] == expected_authority, "fixture or failing evidence cannot claim authoritative full-corpus reproducibility")
    for key in ("session_id", "approval_id", "offline_pack_id", "source_set_root", "environment_id"):
        contracts._hash(value[key], key)
    baseline = exact(value["baseline"], {"release_id", "manifest_sha256"}, "attested baseline")
    contracts._hash(baseline["release_id"], "baseline release_id")
    contracts._digest(baseline["manifest_sha256"], "baseline manifest_sha256")
    compatibility = exact(value["compatibility"], {"mode", "report_sha256", "graph_topology_content", "provenance"}, "attested compatibility")
    require(compatibility["mode"] in {"exact", "reviewed-provenance-only"}, "unknown attested provenance policy")
    contracts._digest(compatibility["report_sha256"], "compatibility report digest")
    require(compatibility["graph_topology_content"] in {"equal", "different"} and
            compatibility["provenance"] in {"equal", "reviewed-only", "different"}, "unknown comparison outcome")
    require(compatibility["mode"] != "exact" or compatibility["provenance"] != "reviewed-only", "exact comparison cannot waive provenance differences")
    require(isinstance(value["builds"], list) and len(value["builds"]) == 2, "attestation requires two builds")
    for build in value["builds"]:
        exact(build, {"generation_id", "container_id", "launch_sha256", "measurement_sha256", "release_id",
                      "output_root", "base_snapshot_id", "projection_id", "semantic_state_root"}, "build identity")
        for key in ("generation_id", "release_id", "output_root", "semantic_state_root"):
            contracts._hash(build[key], key)
        for key in ("container_id", "launch_sha256", "measurement_sha256", "base_snapshot_id", "projection_id"):
            contracts._digest(build[key], key)
    if value["result"] == "pass":
        require(compatibility["graph_topology_content"] == "equal" and compatibility["provenance"] in {"equal", "reviewed-only"},
                "passing attestation requires compatible actual content and provenance")
        first, second = value["builds"]
        require(first["container_id"] != second["container_id"], "pass requires distinct observed containers")
        for key in ("generation_id", "release_id", "output_root", "semantic_state_root", "measurement_sha256", "base_snapshot_id", "projection_id"):
            require(first[key] == second[key], "passing build identities differ")
    require(value["attestation_id"] == identity("wikilean.reproducibility-attestation.v1", value, "attestation_id"),
            "reproducibility attestation identity mismatch")
    return value


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    modes = result.add_subparsers(dest="mode", required=True)
    run = modes.add_parser("run", help="prepare and actually launch two fresh trusted OCI replays")
    for name in ("approval", "baseline", "manifest", "root", "oci-layout", "policy", "wheelhouse", "docker", "socket", "destination"):
        run.add_argument("--" + name, type=Path, required=True)
    for name in ("docker-sha256", "engine-id", "engine-version"):
        run.add_argument("--" + name, required=True)
    run.add_argument("--seed", type=int, default=20260908)
    run.add_argument("--stage-timeout-seconds", type=float, default=runner.DEFAULT_STAGE_TIMEOUT_SECONDS)
    run.add_argument("--timeout-seconds", type=float, default=12 * 60 * 60)
    run.add_argument("--memory-bytes", type=int, default=16 * 1024**3)
    final = modes.add_parser("finalize", help="reverify reviewed session and its two producer-owned real-pack releases")
    for name in ("session", "output"):
        final.add_argument("--" + name, type=Path, required=True)
    final.add_argument("--expected-session-id", required=True)
    diagnostic = modes.add_parser("diagnose", help="compare artifacts only; never issue launch or authority evidence")
    diagnostic.add_argument("--before", type=Path, required=True)
    diagnostic.add_argument("--after", type=Path, required=True)
    return result


def main(argv: list[str] | None = None) -> int:
    try:
        args = parser().parse_args(argv)
        if args.mode == "diagnose":
            report = comparison_report(args.before, args.after)
            result = {"schema": DIAGNOSTIC_SCHEMA, "authority": "none", "report": report}
        else:
            result = run_gate(args) if args.mode == "run" else finalize(args)
        # Diagnostic reports contain exact corpus decimals and source strings;
        # control-document canonicalization intentionally rejects those values.
        print((report_bytes(result) if args.mode == "diagnose" else canonical(result)).decode())
        return 0
    except (OSError, ValueError, RuntimeError) as exc:
        print(canonical({"ok": False, "authoritative": False, "error": {"type": type(exc).__name__, "message": str(exc)}}).decode(), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
