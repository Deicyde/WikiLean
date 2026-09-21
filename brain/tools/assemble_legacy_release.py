#!/usr/bin/env python3
"""Assemble a private compatibility release from separately reviewed legacy bytes.

This verifies correspondence and SQLite/static parity, not native legacy execution
or baseline approval. The release's reducer/builder commit identifies the current
composite assembly generation. Its retained configuration separately binds the
old graph reducer, completed diagnostic record and schema-2 projector. Never use
the old graph commit to identify the new freezer. Run in an isolated workspace;
this helper is not a hostile-same-UID sandbox or an offline replay launcher.
"""
from __future__ import annotations

import argparse
import hashlib
import os
import re
import sqlite3
import stat
import sys
import unicodedata
from pathlib import Path, PurePosixPath

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import build_release as freezer
import legacy_stage_executor as execution_contract
import legacy_sqlite_projection as projection

contracts = projection.contracts
SCHEMA = "wikilean.legacy-release-assembly-plan/v1"
LEGACY_COMMIT = execution_contract.LEGACY_COMMIT
LEGACY_TREE = execution_contract.LEGACY_TREE
LEGACY_PROGRAM_HASHES = dict(execution_contract.PROGRAM_HASHES)
STAGE_RECIPES = execution_contract.STAGES
STAGES = tuple(program for program, _arguments in STAGE_RECIPES)
LEGACY_PROGRAM_PATHS = frozenset({*STAGES, "brain/build_common.py", "brain/store.py",
                                  "brain/layout.py", "brain/frontier_suitability.py"})
PROVENANCE = frozenset({"catalog/data/source_registry.json", "brain/data/community_edges.jsonl"})
SEMANTIC = frozenset(contracts.COMPATIBILITY_SEMANTIC_PATHS)
HALO = "manage/data/halo.json"
PREFIX = ".legacy-compatibility/"
MAX_CONTROL = 64 * 1024**2
MAX_FILE = 16 * 1024**3
PROGRAMS = {**projection.PROGRAMS, "assembler": Path(__file__).resolve(),
            "freezer": Path(freezer.__file__).resolve(),
            "legacy_execution_contract": Path(execution_contract.__file__).resolve()}


def require(value, message):
    if not value:
        raise ValueError(message)


def canonical(value):
    return contracts.canonical_json_bytes(value)


def identity(raw):
    return {"sha256": hashlib.sha256(raw).hexdigest(), "bytes": len(raw)}


def ref(value, *, path=False):
    require(isinstance(value, dict) and {"sha256", "bytes"}.issubset(value), "file reference is incomplete")
    require(isinstance(value["sha256"], str) and re.fullmatch(r"[0-9a-f]{64}", value["sha256"]), "invalid file SHA-256")
    require(type(value["bytes"]) is int and 0 <= value["bytes"] <= MAX_FILE, "invalid bounded file size")
    if path:
        literal(value.get("path"))
    return {key: value[key] for key in ("sha256", "bytes")}


def literal(value):
    require(isinstance(value, str) and value and not any(char in value for char in "*?[]"), "literal file path required")
    contracts.validate_literal_relative_path(value, "legacy assembly path")
    return value


def absolute_posix(value, label):
    require(isinstance(value, str) and value.startswith("/") and "\\" not in value and
            not any(unicodedata.category(char).startswith("C") for char in value),
            label + " must be an absolute normalized POSIX path without controls")
    parts = value.split("/")[1:]
    require(bool(parts) and all(part not in {"", ".", ".."} for part in parts) and
            PurePosixPath(value).as_posix() == value,
            label + " must be an absolute normalized POSIX path without controls")
    return value


def measure(path, expected=None, *, copy_to=None):
    target = None
    try:
        with os.fdopen(projection.open_absolute(path), "rb") as source:
            before = os.fstat(source.fileno())
            require(stat.S_ISREG(before.st_mode) and before.st_size <= MAX_FILE, "input is not a bounded regular file")
            if expected is not None:
                require(before.st_size == ref(expected)["bytes"], "input size differs: " + str(path))
            if copy_to is not None:
                copy_to.parent.mkdir(parents=True, exist_ok=True)
                target = copy_to.open("xb")
            digest = hashlib.sha256(); size = 0
            while chunk := source.read(1024 * 1024):
                size += len(chunk); require(size <= MAX_FILE, "input grew beyond bound")
                digest.update(chunk)
                if target is not None:
                    target.write(chunk)
            require(projection.signature(before) == projection.signature(os.fstat(source.fileno())), "input changed during read")
            actual = {"sha256": digest.hexdigest(), "bytes": size}
            require(expected is None or actual == ref(expected), "input bytes differ: " + str(path))
            if target is not None:
                target.flush(); os.fsync(target.fileno())
            return actual
    finally:
        if target is not None:
            target.close()


LOADED_PROGRAMS = {name: measure(path) for name, path in PROGRAMS.items()}


def read_control(path, expected=None):
    if expected is not None:
        require(ref(expected)["bytes"] <= MAX_CONTROL, "control document exceeds bound")
    with os.fdopen(projection.open_absolute(path), "rb") as handle:
        before = os.fstat(handle.fileno())
        require(stat.S_ISREG(before.st_mode) and before.st_size <= MAX_CONTROL, "control must be a bounded regular file")
        raw = handle.read(MAX_CONTROL + 1)
        require(projection.signature(before) == projection.signature(os.fstat(handle.fileno())), "control changed during read")
    require(len(raw) <= MAX_CONTROL and (expected is None or identity(raw) == ref(expected)), "control bytes differ")
    value = contracts.parse_json_bytes(raw, location=str(path))
    require(raw == canonical(value), "control document is not canonical")
    return value, raw


def plan_id(plan):
    return contracts.domain_hash(SCHEMA, {key: value for key, value in plan.items() if key != "plan_id"})


def validate_plan(plan, expected_id):
    keys = {"schema", "plan_id", "execution", "projection_plan", "projection_report", "provenance",
            "programs", "semantic_epoch", "assembly_git_commit", "curated_authority_git_commit"}
    require(isinstance(plan, dict) and set(plan) == keys and plan["schema"] == SCHEMA, "assembly plan fields differ")
    require(plan["plan_id"] == expected_id == plan_id(plan), "assembly plan ID differs")
    for name in ("execution", "projection_plan", "projection_report"):
        ref(plan[name])
    require(isinstance(plan["provenance"], dict) and set(plan["provenance"]) == PROVENANCE, "both exact sealed provenance inputs are required")
    for value in plan["provenance"].values():
        ref(value)
    require(plan["programs"] == LOADED_PROGRAMS, "assembly programs differ from the loaded complete generation")
    for field in ("assembly_git_commit", "curated_authority_git_commit"):
        require(isinstance(plan[field], str) and re.fullmatch(r"[a-f0-9]{40}", plan[field]), "invalid commit: " + field)
    require(plan["assembly_git_commit"] != LEGACY_COMMIT, "old graph commit cannot identify the new compatibility freezer")
    require(isinstance(plan["semantic_epoch"], str) and plan["semantic_epoch"], "semantic epoch is required")


def records(rows, label):
    require(isinstance(rows, list), label + " must be an array")
    result = {}
    for row in rows:
        ref(row, path=True)
        path = row["path"]
        require(path not in result and not path.startswith(PREFIX), label + " paths repeat or use the assembly namespace")
        result[path] = row
    require(list(result) == sorted(result), label + " must be path-sorted")
    return result


def validate_execution(record):
    require(isinstance(record, dict) and record.get("schema") == "wikilean.legacy-baseline-execution/v1"
            and record.get("scope") == "baseline-diagnostic", "unexpected legacy execution record")
    require(record.get("authority") is False and record.get("baseline_approved") is False and
            record.get("offline_replay_verified") is False, "execution record must remain diagnostic")
    legacy = record.get("legacy", {})
    require(legacy.get("git_commit") == LEGACY_COMMIT and legacy.get("git_tree") == LEGACY_TREE,
            "legacy program generation differs")
    for name in ("offline_pack_id", "source_set_root", "reducer_inventory_id"):
        contracts._hash(record.get("pack", {}).get(name), "legacy record pack." + name)
    programs = records(legacy.get("program_files"), "legacy programs")
    require(set(programs) == LEGACY_PROGRAM_PATHS, "legacy program closure must contain exactly the ten ebac reducer programs")
    require(all(programs[path]["sha256"] == LEGACY_PROGRAM_HASHES[path] for path in LEGACY_PROGRAM_PATHS),
            "legacy program bytes differ from the exact reviewed generation")
    stages = record.get("stages")
    require(isinstance(stages, list) and [row.get("program") for row in stages] == list(STAGES), "legacy stage sequence is incomplete")
    evidence = {}
    interpreters = set()
    prepared_roots = set()
    for stage, (program, arguments) in zip(stages, STAGE_RECIPES, strict=True):
        require(type(stage.get("exit")) is int and stage["exit"] == 0, "legacy stage did not complete")
        argv = stage.get("argv")
        require(isinstance(argv, list) and all(isinstance(arg, str) for arg in argv), "legacy stage argv is missing")
        require(len(argv) == 8 + len(arguments) and
                tuple(argv[1:6]) == execution_contract.PYTHON_STAGE_FLAGS and
                argv[6] == execution_contract.RUN_STAGE and tuple(argv[8:]) == arguments,
                "legacy stage argv differs from the exact isolated recipe")
        absolute_posix(argv[0], "legacy interpreter path")
        suffix = "/code/" + program
        absolute_posix(argv[7], "legacy stage program path")
        require(argv[7].endswith(suffix), "legacy stage program path differs")
        prepared_root = argv[7][:-len(suffix)]
        absolute_posix(prepared_root, "legacy prepared root")
        interpreters.add(argv[0]); prepared_roots.add(prepared_root)
        for kind in ("stdout", "stderr"):
            value = stage.get(kind); ref(value, path=True)
            prior = evidence.setdefault(value["path"], value)
            require(ref(prior) == ref(value), "legacy log aliases disagree")
    require(len(interpreters) == 1 and len(prepared_roots) == 1,
            "legacy stages do not share one interpreter and prepared root")
    for kind in ("configuration", "runtime"):
        value = record.get(kind, {}).get("preimage"); ref(value, path=True)
        prior = evidence.setdefault(value["path"], value)
        require(ref(prior) == ref(value), "legacy preimage aliases disagree")
    supports = record.get("runtime", {}).get("support_files", [])
    require(isinstance(supports, list), "runtime support files must be a list")
    support_paths = []
    for value in supports:
        ref(value, path=True)
        support_paths.append(value["path"])
        prior = evidence.setdefault(value["path"], value)
        require(ref(prior) == ref(value), "runtime support aliases disagree")
    require(support_paths == sorted(set(support_paths)), "runtime support files must have sorted unique paths")
    inputs = records(record.get("inputs"), "legacy inputs")
    for value in inputs.values():
        require({"input_id", "source_manifest_id", "object", "logical_root", "member_path"} <= set(value), "legacy input lineage is incomplete")
        contracts._hash(value["source_manifest_id"], "legacy input source manifest")
        for name in ("input_id", "object", "logical_root"):
            require(isinstance(value[name], str) and value[name], "legacy input lineage name is missing")
        literal(value["member_path"])
    outputs = records(record.get("outputs"), "legacy outputs")
    require(not (set(inputs) & set(outputs) or set(programs) & (set(inputs) | set(outputs))), "legacy input/output/program ownership overlaps")
    require(SEMANTIC | {"brain/data/brain.sqlite3"} <= set(outputs), "completed legacy output lacks semantic files or original SQLite")
    require(PROVENANCE <= set(inputs), "original input inventory lacks sealed provenance")
    halo_group = record.get("halo", {})
    require(isinstance(halo_group, dict) and set(halo_group) == {"output", "program", "report", "preparation"},
            "legacy halo evidence closure differs")
    halo = halo_group.get("output"); ref(halo, path=True)
    require(halo["path"] == HALO and HALO not in programs, "exact generated halo input is required")
    if HALO in inputs:
        require(ref(inputs[HALO]) == ref(halo), "halo input identity disagrees")
    if HALO in outputs:
        require(ref(outputs[HALO]) == ref(halo), "halo output identity disagrees")
    for name in ("program", "report", "preparation"):
        value = halo_group[name]
        ref(value, path=True); prior = evidence.setdefault(value["path"], value)
        require(ref(prior) == ref(value), "halo evidence aliases disagree")
    absences = record.get("absences")
    require(isinstance(absences, list), "explicit legacy absences are required")
    for row in absences:
        require(isinstance(row, dict) and ("path" in row) != ("path_pattern" in row), "invalid legacy absence")
        if "path" in row:
            literal(row["path"])
        else:
            contracts.validate_relative_path(row["path_pattern"], "legacy absence")
    return inputs, outputs, programs, halo, evidence, absences


def check_absences(root, absences):
    for row in absences:
        path = row.get("path", row.get("path_pattern"))
        if "path" in row:
            require(not os.path.lexists(root / path), "recorded absent legacy input is present: " + path)
        else:
            require(not list(root.glob(path)), "recorded absent legacy pattern has members: " + path)


def write(path, raw):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("xb") as handle:
        handle.write(raw); handle.flush(); os.fsync(handle.fileno())


def assemble(legacy_root, projection_root, sealed_root, evidence_root, destination, plan, expected_id,
             execution_path, projection_plan_path):
    validate_plan(plan, expected_id)
    roots = [Path(path) for path in (legacy_root, projection_root, sealed_root, evidence_root, destination)]
    legacy_root, projection_root, sealed_root, evidence_root, destination = roots
    require(all(path.is_absolute() and path.resolve(strict=path != destination) == path for path in roots), "all roots must be real absolute paths")
    require(all(destination != root and destination not in root.parents and root not in destination.parents
                for root in roots[:-1]), "assembly destination must be disjoint from all inputs")
    for name, path in PROGRAMS.items():
        measure(path, plan["programs"][name])
    record, record_raw = read_control(execution_path, plan["execution"])
    inputs, outputs, old_programs, halo, evidence, absences = validate_execution(record)
    projector_plan, projector_plan_raw = read_control(projection_plan_path, plan["projection_plan"])
    projection.validate_plan(projector_plan, projector_plan["plan_id"])
    require(projector_plan["programs"] == projection.LOADED_PROGRAMS, "projection plan does not use the current reviewed projector generation")
    projected, projected_raw = read_control(projection_root / "projection.json", plan["projection_report"])
    require(projected.get("schema") == "wikilean.legacy-sqlite-projection-result/v1" and projected.get("scope") == "projection-only"
            and projected.get("plan_id") == projector_plan["plan_id"] and projected.get("programs") == projector_plan["programs"], "projection report binding differs")
    require(all(projected.get(key) is False for key in ("authority", "baseline_approved", "legacy_execution_verified", "static_release_verified")), "projection report overstates its scope")
    require(projected.get("semantic_bytes_preserved") is True, "projection record is incomplete")
    require(projected.get("indexed_artifacts") == list(projection.store.DEFAULT_ARTIFACT_FILES)
            and projected.get("pass_through_artifacts") == ["frontier.jsonl", "frontier_graph.json"], "projection artifact coverage differs")
    measured = []
    for rows, root in ((inputs, legacy_root), (outputs, legacy_root), (old_programs, legacy_root), (evidence, evidence_root)):
        for path, value in rows.items():
            measure(root / path, value); measured.append((root / path, value))
    measure(legacy_root / HALO, halo); measured.append((legacy_root / HALO, halo))
    check_absences(legacy_root, absences)
    dynamic = freezer._static_closure(legacy_root)
    static = set(contracts.REQUIRED_RELEASE_PATHS) - SEMANTIC - PROVENANCE - {"brain/data/brain.sqlite3"}
    static |= dynamic
    require(static <= set(outputs), "execution output closure is missing static artifacts")
    for path in PROVENANCE:
        require(ref(inputs[path]) == ref(plan["provenance"][path]), "sealed provenance differs from original legacy input")
        measure(sealed_root / path, plan["provenance"][path]); measured.append((sealed_root / path, plan["provenance"][path]))
    for name in projection.INPUT_FILES:
        original = "brain/data/" + name
        require(ref(outputs[original]) == ref(projector_plan["inputs"][name]), "projection changes an original legacy input")
        relative = "retained/brain.sqlite3" if name == "brain.sqlite3" else original
        measure(projection_root / relative, projector_plan["inputs"][name]); measured.append((projection_root / relative, projector_plan["inputs"][name]))
    require(projected["original_sqlite"]["path"] == "retained/brain.sqlite3"
            and ref(projected["original_sqlite"]) == ref(outputs["brain/data/brain.sqlite3"]), "original SQLite report differs")
    old_identity = projection.legacy_index_identity(projection_root / "retained/brain.sqlite3")
    require(all(projected["original_sqlite"].get(key) == value for key, value in old_identity.items()), "retained legacy SQLite identity differs")
    require(projected["new_sqlite"]["path"] == "brain/data/brain.sqlite3" and projected["new_sqlite"]["schema_version"] == 2, "projected SQLite path/schema differs")
    measure(projection_root / "brain/data/brain.sqlite3", projected["new_sqlite"])
    measured.append((projection_root / "brain/data/brain.sqlite3", projected["new_sqlite"]))
    destination.mkdir(mode=0o700)
    workspace = destination / "assembly"; workspace.mkdir(mode=0o700)
    retained = destination / "evidence"; retained.mkdir(mode=0o700)
    retained_files = {}
    def retain(path, raw):
        write(retained / path, raw)
        retained_files[path] = identity(raw)
    def retain_copy(path, source, expected):
        measure(source, expected, copy_to=retained / path)
        retained_files[path] = ref(expected)
    retain("execution.json", record_raw)
    retain("projection-plan.json", projector_plan_raw)
    retain("projection.json", projected_raw)
    retain("assembly-plan.json", canonical(plan))
    for path, value in evidence.items():
        retain_copy("legacy-evidence/" + path, evidence_root / path, value)
    for name, path in PROGRAMS.items():
        retain_copy("implementation/" + name + ".py", path, plan["programs"][name])
    # Materialize every original input/program literally before the v1 freezer
    # hashes the inventory. Missing files cannot turn into false absence claims.
    original_inventory = {**inputs, **old_programs, HALO: halo}
    for path, value in original_inventory.items():
        measure(legacy_root / path, value, copy_to=workspace / path)
    for path in static:
        measure(legacy_root / path, outputs[path], copy_to=workspace / path)
    for path in SEMANTIC | {"brain/data/brain.sqlite3"}:
        expected = projected["new_sqlite"] if path.endswith(".sqlite3") else outputs[path]
        measure(projection_root / path, expected, copy_to=workspace / path)
    for path in set(outputs) - static - SEMANTIC:
        retain_copy("legacy-output/" + path, legacy_root / path, outputs[path])
    if "brain/data/brain.sqlite3" not in outputs:
        raise ValueError("original database retention is incomplete")
    artifacts = {}
    for path in SEMANTIC:
        raw_ref = measure(workspace / path, outputs[path])
        logical_format = "jsonl-rowset" if path.endswith(".jsonl") else "json"
        with (workspace / path).open("rb") as handle:
            logical_root = contracts._artifact_logical_root_handle(handle, logical_format, path)
        artifacts[path] = {"path": path, **raw_ref, "logical_root": logical_root, "logical_format": logical_format}
    require(projected["artifacts"] == artifacts, "projection semantic artifact report differs from actual files")
    with (workspace / "brain/data/brain.sqlite3").open("rb") as handle:
        contracts._verify_sqlite_projection(handle, workspace, artifacts, verify_static_closure=False)
    inventory = {"schema": "wikilean.reducer-input-inventory/v1", "scope": ["legacy original inputs, programs and generated halo"],
        "inputs": [{"path": path, "class": "immutable_source_object", "consumers": ["legacy-ebac34dc"], "purpose": "exact retained original legacy input or program"}
                   for path in sorted(original_inventory)] +
                  [{key: row[key] for key in ("path", "path_pattern") if key in row} for row in absences]}
    inventory_raw = canonical(inventory); inventory_path = PREFIX + "input-inventory.json"
    write(workspace / inventory_path, inventory_raw)
    configuration = {"schema": "wikilean.legacy-composite-configuration/v1", "assembly_plan_id": expected_id,
        "legacy_graph": record["legacy"], "legacy_configuration": record["configuration"],
        "legacy_execution_record": plan["execution"], "projection_plan": plan["projection_plan"],
        "projection_record": plan["projection_report"], "projector_programs": projector_plan["programs"],
        "assembly_programs": plan["programs"], "input_inventory": identity(inventory_raw),
        "identity_note": "release reducer/builder commit denotes this composite assembly; legacy graph commit is bound separately"}
    environment = {"schema": "wikilean.legacy-composite-environment/v1", "scope": "diagnostic-only",
        "legacy_runtime": record["runtime"], "assembly_runtime": {"python": sys.version, "sqlite": sqlite3.sqlite_version,
            "interpreter": measure(Path(sys.executable).resolve(strict=True))}}
    for name, value in (("configuration.json", configuration), ("environment.json", environment)):
        write(workspace / (PREFIX + name), canonical(value)); retain(name, canonical(value))
    def final_check():
        for path, expected in measured:
            measure(path, expected)
        for path, value in original_inventory.items():
            measure(workspace / path, value)
        for path in SEMANTIC | static:
            measure(workspace / path, outputs[path])
        for path in PROVENANCE:
            measure(workspace / path, plan["provenance"][path])
        measure(workspace / "brain/data/brain.sqlite3", projected["new_sqlite"])
        measure(retained / "legacy-output/brain/data/brain.sqlite3", outputs["brain/data/brain.sqlite3"])
        actual_retained = set()
        for directory, names, files in os.walk(retained, followlinks=False):
            for name in names:
                require(not (Path(directory) / name).is_symlink(), "retained evidence contains a symlink directory")
            actual_retained.update((Path(directory) / name).relative_to(retained).as_posix() for name in files)
        require(actual_retained == set(retained_files), "retained evidence file closure differs")
        for path, expected in retained_files.items():
            measure(retained / path, expected)
        check_absences(legacy_root, absences); check_absences(workspace, absences)
        for name, path in PROGRAMS.items():
            measure(path, plan["programs"][name])
    final_check()
    result = freezer.build_release(freezer.BuildConfig(repo_root=workspace, output_store=destination / "releases",
        semantic_epoch=plan["semantic_epoch"], schedule="legacy-compatibility-assembly",
        reducer_version="legacy-ebac34dc+sqlite-v2-compatibility/v1",
        authority_git_commit=plan["curated_authority_git_commit"], reducer_git_commit=plan["assembly_git_commit"],
        configuration_sha256=identity(canonical(configuration))["sha256"], environment_sha256=identity(canonical(environment))["sha256"],
        input_inventory=inventory_path), _before_publish=final_check)
    release_root = Path(result["root"])
    manifest, _ = contracts.load_canonical_json(release_root / "release.json")
    contracts.verify_release_files(contracts.validate_release_manifest(manifest), release_root)
    require(manifest["profile"] == contracts.RELEASE_PROFILE and "replay" not in manifest, "compatibility assembly cannot claim offline replay")
    for path in SEMANTIC:
        measure(release_root / path, outputs[path])
    final_check()
    report = {"schema": "wikilean.legacy-release-assembly-result/v1", "scope": "compatibility-preparation",
        "authority": False, "baseline_approved": False, "legacy_execution_verified": False,
        "offline_replay_verified": False, "static_release_verified": True, "semantic_bytes_preserved": True,
        "plan_id": expected_id, "execution_record": plan["execution"], "projection_record": plan["projection_report"],
        "legacy_graph_git_commit": LEGACY_COMMIT, "composite_assembly_git_commit": plan["assembly_git_commit"],
        "curated_authority_git_commit": plan["curated_authority_git_commit"], "input_inventory": identity(inventory_raw),
        "original_input_count": len(inputs), "original_program_count": len(old_programs), "halo_input": ref(halo),
        "configuration": identity(canonical(configuration)), "environment": identity(canonical(environment)),
        "release": result, "retained_evidence": "evidence",
        "evidence_files": [{"path": path, **value} for path, value in sorted(retained_files.items())], "programs": plan["programs"]}
    write(destination / "assembly.json", canonical(report))
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("plan", "legacy-root", "projection-root", "sealed-root", "evidence-root", "destination", "execution-record", "projection-plan"):
        parser.add_argument("--" + name, required=True, type=Path)
    parser.add_argument("--expected-plan-id", required=True)
    args = parser.parse_args(argv)
    try:
        plan, _ = read_control(args.plan)
        report = assemble(args.legacy_root, args.projection_root, args.sealed_root, args.evidence_root, args.destination,
            plan, args.expected_plan_id, args.execution_record, args.projection_plan)
        sys.stdout.buffer.write(canonical(report))
    except (ValueError, OSError, KeyError, TypeError, sqlite3.Error, projection.store.StoreError, contracts.VerificationError) as exc:
        print("Legacy compatibility assembly failed: " + str(exc), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
