#!/usr/bin/env python3
"""Qualify two real private OCI replays with independently pinned policy evidence.

This separate profile leaves reproducibility-gate/v1 and every source restriction
unchanged. It creates no public-release permission or accepted assertion authority.
The technical launch/measurement helpers are reused; this module owns both launch
calls and the policy checkpoints. No caller-supplied successful run is accepted.
"""
from __future__ import annotations

import argparse
import importlib
import os
import stat
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import reproducibility_gate as technical
import source_policy_reviews as reviews

contracts = technical.contracts
environment = technical.environment
launcher = technical.launcher
oci_runtime = technical.oci_runtime
preparation = technical.preparation
runner = technical.runner
build_replay_release = technical.build_replay_release
require = technical.require
canonical = technical.canonical
sha = technical.sha
identity = technical.identity
exact = technical.exact
read_document = technical.read_document
write_new = technical.write_new
verified_pack = technical.verified_pack
verified_baseline = technical.verified_baseline
verify_context = technical.verify_context
verify_launch_record = technical.verify_launch_record
randomize_mtimes = technical.randomize_mtimes
hostile_environment = technical.hostile_environment
measure_output = technical.measure_output
capture_tree = technical.capture_tree
verified_pack_release = technical.verified_pack_release
comparison_report = technical.comparison_report
report_bytes = technical.report_bytes
verify_compatibility = technical.verify_compatibility

PROFILE = "brain-private-replay-qualified-v1"
APPROVAL_SCHEMA = "wikilean.private-replay-qualification-approval/v1"
SESSION_SCHEMA = "wikilean.private-replay-qualification-session/v1"
ATTESTATION_SCHEMA = "wikilean.private-replay-qualification-attestation/v1"
APPROVAL_DOMAIN = "wikilean.private-replay-qualification-approval.v1"
SESSION_DOMAIN = "wikilean.private-replay-qualification-session.v1"
ATTESTATION_DOMAIN = "wikilean.private-replay-qualification-attestation.v1"
NON_AUTHORITY = {"accepted_authority": False, "public_release_policy_ready": False,
                 "production_activation": False, "reviewer_authenticated": False,
                 "legal_determination": False}
IMPLEMENTATION_PATHS = tuple(sorted(set(technical.IMPLEMENTATION_PATHS) | {
    "brain/tools/private_replay_gate.py", "brain/tools/source_policy_reviews.py"}))
MAX_ATTACHMENT_TOTAL = 256 * 1024 * 1024


def measured_implementation():
    require(Path(technical.__file__).resolve() == HERE / "reproducibility_gate.py" and
            Path(reviews.__file__).resolve() == HERE / "source_policy_reviews.py" and
            reviews.contracts is contracts, "private qualification helper origin differs")
    return [{"path": path, "sha256": environment.secure_file_digest(HERE.parent.parent / path)[0]}
            for path in IMPLEMENTATION_PATHS]


LOADED_IMPLEMENTATION = tuple((item["path"], item["sha256"]) for item in measured_implementation())


def implementation():
    current = measured_implementation()
    require(tuple((item["path"], item["sha256"]) for item in current) == LOADED_IMPLEMENTATION,
            "private qualification helper bytes differ from the loaded implementation")
    return current


def validate_approval(value, expected_id):
    fields = {"schema", "approval_id", "scope", "offline_pack_id", "source_set_root",
              "reducer_inventory_id", "environment_id", "authority_git_commit", "authority_root",
              "semantic_epoch", "prior_state_root", "baseline", "provenance", "profile", "private_policy"}
    exact(value, fields, "private qualification approval")
    require(value["schema"] == APPROVAL_SCHEMA and value["profile"] == PROFILE, "unsupported private qualification profile")
    require(value["approval_id"] == expected_id == identity(APPROVAL_DOMAIN, value, "approval_id"),
            "qualification approval differs from independently expected ID")
    # Preserve every existing technical baseline/runtime/scope rule without
    # treating the projection as a new user approval or publishing a v1 record.
    projected = {key: item for key, item in value.items() if key not in {"profile", "private_policy"}}
    projected["schema"] = technical.APPROVAL_SCHEMA
    projected["approval_id"] = identity("wikilean.reproducibility-approval.v1", projected, "approval_id")
    technical.validate_approval(projected)
    policy = exact(value["private_policy"], {"review_id", "review_sha256", "attachment_root", "attachment_files"}, "approved private policy")
    contracts._hash(policy["review_id"], "private review ID")
    contracts._digest(policy["review_sha256"], "private review bytes")
    contracts._hash(policy["attachment_root"], "private attachment closure")
    require(type(policy["attachment_files"]) is int and 0 <= policy["attachment_files"] <= reviews.MAX_EVIDENCE,
            "invalid private attachment count")
    return value


def attachment_records(review):
    records = {}
    for item in review["evidence"]:
        if item["kind"] != "review-attachment":
            continue
        record = {key: item[key] for key in ("path", "sha256", "bytes")}
        require(item["path"] not in records or canonical(records[item["path"]]) == canonical(record),
                "conflicting private attachment path")
        records[item["path"]] = record
    values = [records[key] for key in sorted(records)]
    require(sum(item["bytes"] for item in values) <= MAX_ATTACHMENT_TOTAL, "private policy attachment closure exceeds bound")
    return values


def policy_binding(review, raw):
    records = attachment_records(review)
    return {"review_id": review["review_id"], "review_sha256": sha(raw),
            "attachment_root": contracts.domain_hash("private-replay-policy-attachments/v1", records),
            "attachment_files": len(records)}


def verify_policy(review_path, attachment_root, pack_path, approval, expected_private_id):
    raw = reviews.secure_read(review_path)
    document = contracts.parse_json_bytes(raw, location="private replay review")
    require(raw == canonical(document), "private replay review is not canonical")
    require(expected_private_id == approval["private_policy"]["review_id"], "private policy ID differs from qualification approval")
    result = reviews.validate_private(document, pack_path, expected_id=expected_private_id, attachment_root=attachment_root)
    require(result["private_replay_policy_ready"] is True, "private replay policy review remains pending or rejected")
    require(result["offline_pack_id"] == approval["offline_pack_id"] and
            canonical(policy_binding(document, raw)) == canonical(approval["private_policy"]),
            "private policy pack, review bytes or attachment closure differs from approval")
    return document, raw


def private_directory(path):
    launcher._real_path(path, directory=True)
    value = path.stat()
    require(value.st_uid == os.getuid() and stat.S_IMODE(value.st_mode) == 0o700,
            "retained policy directories must be current-user-owned mode0700")


def retain_policy(root, review, raw, attachment_root):
    destination = root / "private-policy"
    destination.mkdir(mode=0o700)
    attachments = destination / "attachments"
    attachments.mkdir(mode=0o700)
    write_new(destination / "review.json", raw)
    for item in attachment_records(review):
        target = attachments / item["path"]
        parent = attachments
        for component in Path(item["path"]).parts[:-1]:
            parent = parent / component
            if not parent.exists():
                parent.mkdir(mode=0o700)
            private_directory(parent)
        data = reviews.secure_read(Path(attachment_root) / item["path"], limit=reviews.MAX_ATTACHMENT_BYTES)
        require(sha(data) == item["sha256"] and len(data) == item["bytes"], "private attachment changed before retention")
        write_new(target, data)
    preparation._fsync_directory(destination)
    return destination


def verify_retained_policy(root, pack_path, approval, expected_private_id):
    destination = root / "private-policy"
    private_directory(destination)
    attachment_root = destination / "attachments"
    private_directory(attachment_root)
    review, raw = verify_policy(destination / "review.json", attachment_root, pack_path, approval, expected_private_id)
    expected = {"review.json", *["attachments/" + item["path"] for item in attachment_records(review)]}
    found = set()
    directories = {"attachments"}
    for name in expected:
        directories.update(str(parent) for parent in Path(name).parents if str(parent) != ".")
    found_directories = set()
    for directory, names, files in os.walk(destination, followlinks=False):
        parent = Path(directory)
        private_directory(parent)
        for name in names:
            child = parent / name
            private_directory(child)
            found_directories.add(child.relative_to(destination).as_posix())
        for name in files:
            path = parent / name
            metadata = path.lstat()
            require(stat.S_ISREG(metadata.st_mode) and metadata.st_nlink == 1 and
                    metadata.st_uid == os.getuid() and stat.S_IMODE(metadata.st_mode) == 0o400,
                    "retained policy members must be owned single-link mode0400 files")
            found.add(path.relative_to(destination).as_posix())
    require(found == expected and found_directories == directories, "retained private policy closure has missing or extra entries")
    return review, raw


def coverage_implementation():
    # The mapping is created after a real release exists. It is independently
    # pinned at finalize(), never guessed or approved by run(). Missing support
    # is a hard pending prerequisite, not an affirmative placeholder report.
    try:
        coverage = importlib.import_module("provenance_coverage")
    except ImportError as exc:
        raise technical.GateError("provenance coverage integration is not available; qualification remains pending") from exc
    require(Path(coverage.__file__).resolve() == HERE / "provenance_coverage.py", "coverage helper origin differs")
    require(callable(getattr(coverage, "implementation", None)) and
            callable(getattr(coverage, "implementation_root", None)) and hasattr(coverage, "IMPLEMENTATION_PATHS"),
            "complete loaded provenance implementation integration is not available; qualification remains pending")
    loaded = coverage.implementation()
    require(isinstance(loaded, tuple) and all(isinstance(item, tuple) and len(item) == 3 for item in loaded),
            "coverage must expose its immutable loaded path/digest/size tuple")
    files = [{"path": path, "sha256": digest, "bytes": size} for path, digest, size in sorted(loaded)]
    validate_coverage_implementation({"files": files,
        "root": contracts.domain_hash("private-replay-coverage-implementation/v1", files)})
    require([item["path"] for item in files] == sorted(coverage.IMPLEMENTATION_PATHS), "coverage closure differs from its declared implementation")
    actual = []
    for path in sorted(coverage.IMPLEMENTATION_PATHS):
        digest, size = environment.secure_file_digest(HERE.parent.parent / path)
        actual.append({"path": path, "sha256": digest, "bytes": size})
    require(files == actual, "coverage helper measurements differ")
    require(coverage.implementation_root() == contracts.domain_hash("provenance-coverage-implementation/v1", files),
            "coverage implementation root differs from its complete measured closure")
    return coverage, {"files": files, "root": contracts.domain_hash("private-replay-coverage-implementation/v1", files)}


def validate_coverage_implementation(value):
    exact(value, {"files", "root"}, "complete coverage implementation")
    files = value["files"]
    require(isinstance(files, list) and 2 <= len(files) <= 64, "invalid coverage implementation closure")
    paths = []
    for item in files:
        exact(item, {"path", "sha256", "bytes"}, "coverage implementation member")
        contracts.validate_literal_relative_path(item["path"], "coverage implementation path")
        contracts._digest(item["sha256"], "coverage implementation digest")
        contracts._expect_int(item["bytes"], "coverage implementation size")
        paths.append(item["path"])
    require(paths == sorted(set(paths)) and {"brain/tools/provenance_coverage.py",
            "brain/tools/provenance_coverage_families.py"} <= set(paths), "coverage implementation omits an executed helper")
    require(value["root"] == contracts.domain_hash("private-replay-coverage-implementation/v1", files),
            "coverage implementation root differs")


def checked_coverage(args, pack_path, release_path, review, attachments):
    coverage, before = coverage_implementation()
    mapping_raw = reviews.secure_read(args.coverage_mapping)
    mapping = contracts.parse_json_bytes(mapping_raw, location="coverage mapping")
    require(mapping_raw == canonical(mapping), "coverage mapping must be canonical")
    report = coverage.check(mapping, pack_path, release_path, review,
        expected_mapping_id=args.expected_coverage_mapping_id,
        expected_private_id=args.expected_private_review_id, private_attachment_root=attachments)
    coverage.validate_report(report)
    require(report["mapping_id"] == args.expected_coverage_mapping_id and
            report["private_review_id"] == args.expected_private_review_id and
            report["public_review_id"] is None and
            mapping["implementation_root"] == report["implementation_root"] == coverage.implementation_root(),
            "coverage report differs from the expected review or implementation")
    require(report.get("provenance_coverage_ready") is True, "complete release provenance coverage remains unresolved")
    require(coverage_implementation()[1] == before and
            reviews.secure_read(args.coverage_mapping) == mapping_raw, "coverage implementation or mapping changed")
    return mapping_raw, report, before


def validate_attestation(value):
    fields = {"schema", "profile", "attestation_id", "result", "scope", "private_replay_qualified",
              "limits", "session_id", "approval_id", "private_policy", "coverage",
              "offline_pack_id", "source_set_root", "environment_id", "baseline", "compatibility", "builds"}
    exact(value, fields, "private replay qualification")
    require(value["schema"] == ATTESTATION_SCHEMA and value["profile"] == PROFILE and
            canonical(value["limits"]) == canonical(NON_AUTHORITY), "qualification exceeds private profile")
    require(type(value["private_replay_qualified"]) is bool and value["private_replay_qualified"] ==
            (value["result"] == "pass" and value["scope"] == "full-corpus"), "incorrect private qualification scope")
    projected = {key: value[key] for key in ("result", "scope", "session_id", "approval_id", "offline_pack_id",
        "source_set_root", "environment_id", "baseline", "compatibility", "builds")}
    projected.update(schema=technical.ATTESTATION_SCHEMA, attestation_id="", authority=
        "full-corpus" if value["scope"] == "full-corpus" and value["result"] == "pass" else
        "diagnostic-fixture" if value["scope"] == "fixture" else "none")
    projected["attestation_id"] = identity("wikilean.reproducibility-attestation.v1", projected, "attestation_id")
    technical.validate_attestation(projected)
    binding = exact(value["private_policy"], {"review_id", "review_sha256", "attachment_root", "attachment_files"}, "attested private policy")
    contracts._hash(binding["review_id"], "review ID"); contracts._digest(binding["review_sha256"], "review SHA")
    contracts._hash(binding["attachment_root"], "attachment closure")
    require(type(binding["attachment_files"]) is int and 0 <= binding["attachment_files"] <= reviews.MAX_EVIDENCE, "invalid attachment count")
    coverage = exact(value["coverage"], {"mapping_id", "mapping_sha256", "report_id", "report_sha256", "implementation"}, "attested coverage")
    for name in ("mapping_id", "report_id"): contracts._hash(coverage[name], name)
    for name in ("mapping_sha256", "report_sha256"): contracts._digest(coverage[name], name)
    validate_coverage_implementation(coverage["implementation"])
    require(value["attestation_id"] == identity(ATTESTATION_DOMAIN, value, "attestation_id"), "private qualification ID mismatch")
    return value


def run_gate(args: argparse.Namespace) -> dict:
    initial_implementation = implementation()
    runner.require_isolated_startup()
    require(sys.platform.startswith("linux"), "prerequisite missing: native Linux trusted OCI runtime; this host cannot issue replay evidence")
    approval, approval_raw = read_document(args.approval)
    validate_approval(approval, args.expected_approval_id)
    require(args.manifest.parent == args.root, "private policy requires the manifest at its verified pack root")
    review, review_raw = verify_policy(args.private_review, args.private_attachments, args.manifest, approval, args.expected_private_review_id)
    verified_baseline(args.baseline, approval)
    pack, descriptor = verified_pack(args.manifest, args.root, approval)
    policy, _raw = read_document(args.policy)
    image = oci_runtime.verify_image(args.oci_layout, descriptor["runtime"]["manifest_digest"], policy, args.wheelhouse, descriptor)
    destination = args.destination
    launcher._real_path(destination.parent, directory=True)
    require(not destination.exists() and not destination.is_symlink(), "gate destination must be fresh")
    sources = [args.root, args.baseline.parent, args.oci_layout, args.wheelhouse]
    if args.private_attachments is not None:
        sources.append(args.private_attachments)
    for source in sources:
        require(not (destination == source or destination in source.parents or source in destination.parents), "gate store overlaps immutable input")
    destination.mkdir(mode=0o700)
    runtime = {key: str(getattr(args, key)) for key in ("manifest", "root", "oci_layout", "policy", "wheelhouse", "docker", "socket")}
    runtime["pack_root"] = runtime.pop("root")
    runtime.update({key: getattr(args, key) for key in ("docker_sha256", "engine_id", "engine_version", "stage_timeout_seconds", "timeout_seconds", "memory_bytes")})
    # Canonical control documents exclude floating-point numbers. Preserve the
    # launcher's exact float argument spelling as a bounded decimal string.
    for key in ("stage_timeout_seconds", "timeout_seconds"):
        runtime[key] = str(runtime[key])
    require(implementation() == initial_implementation, "loaded gate implementation changed during input verification")
    write_new(destination / "approval.json", approval_raw)
    write_new(destination / "runtime.json", canonical(runtime))
    retain_policy(destination, review, review_raw, args.private_attachments)
    verify_retained_policy(destination, args.manifest, approval, args.expected_private_review_id)
    builds = []
    try:
        for index, seed in enumerate((args.seed, args.seed ^ 0x5DEECE66D)):
            verify_retained_policy(destination, args.manifest, approval, args.expected_private_review_id)
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
            verify_retained_policy(destination, args.manifest, approval, args.expected_private_review_id)
            require(implementation() == initial_implementation, "private gate implementation changed before launch")
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
        verify_retained_policy(destination, args.manifest, approval, args.expected_private_review_id)
        session = {"schema": SESSION_SCHEMA, "profile": PROFILE, "private_policy": approval["private_policy"], "limits": NON_AUTHORITY, "session_id": "", "status": "pending-attestation", "scope": approval["scope"],
                   "approval_id": approval["approval_id"], "baseline_manifest": str(args.baseline),
                   "runtime_sha256": sha(canonical(runtime)), "implementation": initial_implementation, "builds": builds,
                   "remaining": ["explicit review of this exact session identity and final evidence re-verification", "complete release-specific provenance mapping and coverage re-verification"]}
        session["session_id"] = identity(SESSION_DOMAIN, session, "session_id")
        write_new(destination / "session.json", canonical(session))
        return session
    except BaseException as exc:
        write_new(destination / "failure.json", canonical({"schema": SESSION_SCHEMA, "status": "failed", "authoritative": False,
            "completed_builds": len(builds), "error": {"type": type(exc).__name__, "message": str(exc)}}))
        raise


def finalize(args: argparse.Namespace) -> dict:
    implementation()
    session, session_raw = read_document(args.session)
    exact(session, {"schema", "profile", "private_policy", "limits", "session_id", "status", "scope",
                    "approval_id", "baseline_manifest", "runtime_sha256", "implementation", "builds", "remaining"},
          "private replay session")
    require(session.get("schema") == SESSION_SCHEMA and session.get("status") == "pending-attestation", "a complete pending-attestation session is required")
    require(session.get("profile") == PROFILE and canonical(session.get("limits")) == canonical(NON_AUTHORITY), "session private profile differs")
    require(session.get("session_id") == args.expected_session_id == identity(SESSION_DOMAIN, session, "session_id"),
            "session differs from explicitly reviewed exact evidence generation")
    require(session["implementation"] == implementation(), "session was produced by a different gate implementation")
    root = args.session.parent
    approval, approval_raw = read_document(root / "approval.json")
    validate_approval(approval, session["approval_id"])
    require(canonical(session["private_policy"]) == canonical(approval["private_policy"]), "session policy binding differs")
    require(session["approval_id"] == approval["approval_id"] and session["scope"] == approval["scope"], "session approval mismatch")
    runtime, runtime_raw = read_document(root / "runtime.json")
    require(sha(runtime_raw) == session["runtime_sha256"], "session runtime changed")
    pack, descriptor = verified_pack(Path(runtime["manifest"]), Path(runtime["pack_root"]), approval)
    require(Path(runtime["manifest"]).parent == Path(runtime["pack_root"]), "private policy pack root differs")
    review, _ = verify_retained_policy(root, Path(runtime["manifest"]), approval, args.expected_private_review_id)
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
        verify_retained_policy(root, Path(runtime["manifest"]), approval, args.expected_private_review_id)
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
    review, _ = verify_retained_policy(root, Path(runtime["manifest"]), approval, args.expected_private_review_id)
    mapping_raw, coverage, coverage_implementation_record = checked_coverage(args, Path(runtime["manifest"]),
        Path(session["builds"][0]["release"]["manifest"]), review, root / "private-policy/attachments")
    # Both release identities/bytes were already checked equal. Coverage of this
    # exact release therefore covers both owned technical replay outputs.
    verify_retained_policy(root, Path(runtime["manifest"]), approval, args.expected_private_review_id)
    require(session["implementation"] == implementation(), "qualification implementation changed during coverage")
    for output_root, entries, release_path, release, manifest_sha256 in stability_checks:
        require(capture_tree(output_root) == entries, "owned replay output changed during coverage")
        require(environment.secure_file_digest(release_path)[0] == manifest_sha256,
                "produced release manifest changed during coverage")
        contracts.verify_release_files(release, release_path.parent)
    verified_pack(Path(runtime["manifest"]), Path(runtime["pack_root"]), approval)
    verified_baseline(Path(session["baseline_manifest"]), approval)
    require(environment.secure_file_digest(args.session)[0] == sha(session_raw), "reviewed session changed during coverage")
    require(environment.secure_file_digest(root / "approval.json")[0] == sha(approval_raw) and
            environment.secure_file_digest(root / "runtime.json")[0] == sha(runtime_raw),
            "retained approval or runtime changed during final verification")
    require(coverage_implementation()[1] == coverage_implementation_record, "coverage implementation changed before final publication")
    attestation = {"schema": ATTESTATION_SCHEMA, "profile": PROFILE, "attestation_id": "", "result": "pass", "scope": approval["scope"],
                   "private_replay_qualified": approval["scope"] == "full-corpus", "limits": NON_AUTHORITY,
                   "private_policy": approval["private_policy"],
                   "coverage": {"mapping_id": args.expected_coverage_mapping_id, "mapping_sha256": sha(mapping_raw),
                       "report_id": coverage["report_id"], "report_sha256": sha(canonical(coverage)), "implementation": coverage_implementation_record},
                   "session_id": session["session_id"], "approval_id": approval["approval_id"],
                   "offline_pack_id": pack["offline_pack_id"], "source_set_root": pack["source_set_root"],
                   "environment_id": descriptor["environment_id"], "baseline": approval["baseline"],
                   "compatibility": session["builds"][0]["compatibility"], "builds": observed}
    attestation["attestation_id"] = identity(ATTESTATION_DOMAIN, attestation, "attestation_id")
    validate_attestation(attestation)
    destination = args.destination
    private_directory(destination.parent)
    require(not destination.exists() and not destination.is_symlink(), "qualification output must be fresh")
    for source in (root, Path(runtime["pack_root"]), Path(session["baseline_manifest"]).parent):
        require(not (destination == source or destination in source.parents or source in destination.parents), "qualification output overlaps retained input")
    destination.mkdir(mode=0o700)
    write_new(destination / "coverage-mapping.json", mapping_raw)
    write_new(destination / "coverage-report.json", canonical(coverage))
    write_new(destination / "qualification.json", canonical(attestation))
    return attestation


def parser():
    result = argparse.ArgumentParser(description=__doc__)
    modes = result.add_subparsers(dest="mode", required=True)
    run = modes.add_parser("run", help="own two real private OCI builds after exact approved private review")
    for name in ("approval", "baseline", "manifest", "root", "oci-layout", "policy", "wheelhouse", "docker", "socket", "destination", "private-review"):
        run.add_argument("--" + name, type=Path, required=True)
    run.add_argument("--private-attachments", type=Path)
    for name in ("docker-sha256", "engine-id", "engine-version", "expected-approval-id", "expected-private-review-id"):
        run.add_argument("--" + name, required=True)
    run.add_argument("--seed", type=int, default=20260908)
    run.add_argument("--stage-timeout-seconds", type=float, default=runner.DEFAULT_STAGE_TIMEOUT_SECONDS)
    run.add_argument("--timeout-seconds", type=float, default=12*60*60)
    run.add_argument("--memory-bytes", type=int, default=16*1024**3)
    final = modes.add_parser("finalize", help="reverify exact session plus release-specific coverage; no production approval")
    for name in ("session", "destination", "coverage-mapping"):
        final.add_argument("--" + name, type=Path, required=True)
    for name in ("expected-session-id", "expected-private-review-id", "expected-coverage-mapping-id"):
        final.add_argument("--" + name, required=True)
    return result


def main(argv=None):
    try:
        args = parser().parse_args(argv)
        value = run_gate(args) if args.mode == "run" else finalize(args)
        print(canonical(value).decode())
        return 0
    except (OSError, ValueError, RuntimeError, KeyError, TypeError) as exc:
        print(canonical({"ok": False, "private_replay_qualified": False, **NON_AUTHORITY,
            "error": {"type": type(exc).__name__, "message": str(exc)}}).decode(), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
