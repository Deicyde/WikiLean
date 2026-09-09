#!/usr/bin/env python3
"""Produce a private old-reducer diagnostic through an inspected local Linux OCI engine.

The exact v3 pack is mandatory for execution. The separate probe command runs a
synthetic kernel test and cannot publish a legacy execution record. Unsigned
records require operator review; neither command grants replay authority or
baseline approval. Container inspection is retained separately from graph facts.
"""
from __future__ import annotations

import argparse
import math
import os
from pathlib import Path
import platform
import sys
import tempfile
import uuid

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import legacy_stage_executor as child
import launch_replay_oci as launcher
import prepare_legacy_baseline as preparer
import oci_runtime

PROGRAMS = {**launcher.runner.RUNNER_FILES,
            "brain/tools/run_legacy_baseline.py": Path(__file__).resolve(),
            "brain/tools/legacy_stage_executor.py": Path(child.__file__).resolve(),
            "brain/tools/legacy_halo_projection.py": HERE / "legacy_halo_projection.py",
            "brain/tools/prepare_legacy_baseline.py": Path(preparer.__file__).resolve(),
            "brain/ingest/git_snapshot.py": Path(preparer.git_snapshot.__file__).resolve()}
LOADED_PROGRAMS = {name: child.measure(path) for name, path in PROGRAMS.items()}


def implementation():
    current = {name: child.measure(path) for name, path in PROGRAMS.items()}
    child.require(current == LOADED_PROGRAMS, "legacy producer implementation changed after import")
    return [{"path": name, **value} for name, value in sorted(current.items())]


def verified_preparation(args):
    record = child.verify_preparation(args.prepared, {"sha256": args.preparation_sha256,
        "bytes": args.preparation_bytes}, cold=True)
    pack, _raw, inventory, _sources, objects = preparer.loaded_pack(args.manifest, args.pack_root, args.expected_pack_id)
    inputs, absences = preparer.input_plan(pack, inventory, objects)
    child.require(record["inputs"] == inputs and record["absences"] == absences, "prepared input closure differs from verified pack")
    child.require(record["pack"] == {"offline_pack_id": pack["offline_pack_id"],
        "source_set_root": pack["source_set_root"], "reducer_inventory_id": pack["inventory"]["inventory_id"]},
        "preparation belongs to another pack")
    raw = preparer.contracts.verify_file_ref(args.pack_root, pack["configuration"], "legacy configuration")
    config = preparer.build_context.ReducerConfiguration.from_document(
        preparer.contracts.parse_json_bytes(raw, location="legacy configuration"))
    child.require(record["configuration"] == {"attach": ["generalization", "special_case"],
        "layout_enabled": True, "layout_iterations": 200, "external_node_cap": config.external_node_cap,
        "staging_mtime_epoch_seconds": child.MTIME_NS // 10**9,
        "pack_configuration_sha256": child.identity(raw)["sha256"]} and
        config.cell_attach_kinds == ("generalization", "special_case") and config.layout_enabled and config.layout_iterations == 200,
        "legacy configuration does not correspond to the verified pack")
    return record


def create_arguments(image, prepared, run, policy, *, uid, gid, name, memory_bytes, engine_image_id):
    # Reuse the inspected engine policy, replacing only its fixed mount/command
    # closure. The shared verifier takes these exact returned expectations.
    arguments, _old_child, _old_mounts = launcher.create_arguments(image, run, prepared, policy,
        uid=uid, gid=gid, name=name, memory_bytes=memory_bytes, engine_image_id=engine_image_id)
    first_mount = arguments.index("--mount")
    arguments = arguments[:first_mount]
    mounts = [{"Source": str(run), "Destination": str(run), "RW": False},
              {"Source": str(run / "evidence"), "Destination": str(run / "evidence"), "RW": True},
              {"Source": str(prepared), "Destination": str(prepared), "RW": False},
              {"Source": str(prepared / "output"), "Destination": str(prepared / "output"), "RW": True},
              {"Source": str(prepared / "scratch"), "Destination": str(prepared / "scratch"), "RW": True}]
    command = ["-i", *[key + "=" + value for key, value in sorted(launcher.launch_environment(policy).items())],
               policy["python"], "-I", "-B", str(run / "tool/legacy_stage_executor.py")]
    for mount in mounts:
        value = "type=bind,src=" + mount["Source"] + ",dst=" + mount["Destination"] + ",bind-propagation=rprivate"
        if not mount["RW"]:
            value += ",readonly"
        arguments += ["--mount", value]
    return [*arguments, engine_image_id, *command], command, mounts


def prepare_probe(root):
    root.mkdir(mode=0o700)
    for relative in ("code", "input", "support", "scratch", "output"):
        (root / relative).mkdir()
    for relative in child.OUTPUT_DIRS:
        (root / "code" / relative).mkdir(parents=True, exist_ok=True)
        (root / "output" / relative).mkdir(parents=True, exist_ok=True)
    raw = b"synthetic readonly input\n"
    program = b'''import json,os
from pathlib import Path
import numpy
from probe_helper import VALUE
root=Path(__file__).resolve().parents[1]
assert os.environ["BRAIN_MATHLIB_CHECKOUT"]==str(root/"inputs/mathlib/Mathlib")
assert os.environ["BRAIN_DECL_ORACLE"]==str(root/"inputs/decl_oracle/declaration-data.json")
assert (root/"brain/data/probe-input.jsonl").read_bytes()==b"synthetic readonly input\\n"
result={"helper":VALUE,"numpy":numpy.__version__,"sum":float(numpy.array([1.,2.,3.,4.]).sum()),
        "external_node_cap":os.environ["BRAIN_EXT_NODE_CAP"],"fixture_only":True}
(root/"brain/data/fixture-bootstrap.json").write_text(json.dumps(result,sort_keys=True))
print(json.dumps(result,sort_keys=True))
'''
    child.write(root / "code/brain/probe.py", program)
    child.write(root / "code/brain/probe_helper.py", b"VALUE='captured-helper'\n")
    child.write(root / "code/brain/data/probe-input.jsonl", raw)
    child.write(root / "input/brain/data/probe-input.jsonl", raw)
    return {"inputs": [{"path": "brain/data/probe-input.jsonl", **child.identity(raw)}],
            "configuration": {"external_node_cap": 32000, "fixture_only": True}}


def materialize_completed(prepared, destination, record, outputs):
    """A fresh literal old layout for the correspondence-only release assembler."""
    destination.mkdir(mode=0o700)
    for row in [*record["legacy"]["program_files"], *record["inputs"]]:
        child.measure(prepared / "code" / row["path"], row, copy_to=destination / row["path"])
        os.utime(destination / row["path"], ns=(child.MTIME_NS, child.MTIME_NS))
    for row in outputs:
        child.measure(prepared / "output" / row["path"], row, copy_to=destination / row["path"])
    child.check_absences(destination, record["absences"])
    child.require(set(child.files(destination)) == {row["path"] for row in
        [*record["legacy"]["program_files"], *record["inputs"], *outputs]}, "completed old layout closure differs")


def run(args):
    launcher.runner.require_isolated_startup()
    child.require(sys.platform.startswith("linux") and sys.version_info[:2] == (3, 12), "native Linux CPython 3.12 host required")
    child.require(math.isfinite(args.timeout_seconds) and 0 < args.timeout_seconds <= 7 * 86400 and
                  math.isfinite(args.stage_timeout_seconds) and 0 < args.stage_timeout_seconds <= 7 * 86400,
                  "bounded finite launch and stage deadlines required")
    initial_programs = implementation()
    destination = args.destination
    child.real(destination.parent)
    child.require(destination.is_absolute() and not os.path.lexists(destination), "a fresh absolute private destination is required")
    policy, raw_policy = oci_runtime.read_control(args.policy)
    descriptor, raw_descriptor = oci_runtime.read_control(args.environment)
    child.require(raw_policy == child.canonical(policy) and raw_descriptor == child.canonical(descriptor), "canonical runtime controls required")
    child.require(descriptor["environment_id"] == args.expected_environment_id, "reviewed runtime reference differs")
    image = oci_runtime.verify_image(args.oci_layout, descriptor["runtime"]["manifest_digest"], policy, args.wheelhouse, descriptor)
    child.require(platform.machine().lower() == image.architecture, "emulated baseline is forbidden")
    apparmor = oci_runtime.apparmor_runtime.verify_loaded(policy["apparmor"], args.wheelhouse) if "apparmor" in policy else None
    apparmor_name = policy["apparmor"]["name"] if apparmor is not None else None
    if args.command == "execute":
        prepared = launcher._real_path(args.prepared, directory=True)
        for source in (prepared, args.pack_root):
            child.require(not (source == destination or source in destination.parents or destination in source.parents),
                          "run destination must be disjoint from prepared and sealed roots")
        record = verified_preparation(args)
    destination.mkdir(mode=0o700)
    run_root = destination / "launch"
    run_root.mkdir(mode=0o700)
    (run_root / "evidence").mkdir(mode=0o700)
    evidence = run_root / "evidence"
    if args.command == "probe":
        prepared = destination / "probe-prepared"
        record = prepare_probe(prepared)
    nonce = uuid.uuid4().hex
    child.write(run_root / "host-sentinel", nonce.encode())
    for name in ("legacy_stage_executor.py", "legacy_halo_projection.py"):
        child.measure(HERE / name, LOADED_PROGRAMS["brain/tools/" + name], copy_to=run_root / "tool" / name)
    for row in initial_programs:
        child.measure(PROGRAMS[row["path"]], row, copy_to=evidence / "producer" / row["path"])
    child.write(evidence / "runtime-policy.json", raw_policy)
    child.write(evidence / "runtime-reference.json", raw_descriptor)
    request = {"schema": "wikilean.legacy-container-request/v1", "nonce": nonce,
        "mode": "legacy-stages" if args.command == "execute" else "kernel-probe",
        "prepared": str(prepared), "run": str(run_root), "policy": policy,
        "executor": LOADED_PROGRAMS["brain/tools/legacy_stage_executor.py"],
        "halo_projector": LOADED_PROGRAMS["brain/tools/legacy_halo_projection.py"],
        "expected_python": descriptor["python"], "stage_timeout": args.stage_timeout_seconds}
    if args.command == "execute":
        request["preparation"] = {"sha256": args.preparation_sha256, "bytes": args.preparation_bytes}
        child.measure(prepared / "preparation.json", request["preparation"], copy_to=evidence / "preparation.json")
    else:
        request["probe_preparation"] = record
    encoded = child.canonical(request)
    child.write(evidence / "container-request.json", encoded)
    with tempfile.TemporaryDirectory(prefix="wikilean-legacy-engine-") as temporary:
        private = Path(temporary).resolve()
        (private / "config.json").write_bytes(b"{}")
        engine = launcher.LocalEngine(args.docker, args.docker_sha256, args.socket, private)
        info = engine.document(["info", "--format", "{{json .}}"])
        child.require(info.get("ID") == args.engine_id and info.get("ServerVersion") == args.engine_version and
                      info.get("OSType") == "linux", "local engine differs from reviewed identity")
        image_before, selected = launcher.inspect_engine_image(engine, image)
        name = "wikilean-replay-legacy-" + nonce
        command, inner, mounts = create_arguments(image, prepared, run_root, policy, uid=os.getuid(), gid=os.getgid(),
            name=name, memory_bytes=args.memory_bytes, engine_image_id=selected)
        cid = None
        try:
            _code, stdout, stderr = engine.command(command)
            child.require(not stderr, "unexpected container create diagnostic")
            cid = stdout.decode("ascii").strip()
            child.require(launcher.CID_RE.fullmatch(cid) is not None, "invalid container identity")
            before = engine.document(["container", "inspect", cid], singleton=True)
            child.write(evidence / "container-created.json", child.canonical(before))
            kwargs = dict(image=image, child=inner, mounts=mounts, uid=os.getuid(), gid=os.getgid(),
                memory_bytes=args.memory_bytes, cid=cid, apparmor_profile=apparmor_name, engine_image_id=selected)
            launcher.verify_container(before, status="created", **kwargs)
            if apparmor is not None:
                child.require(oci_runtime.apparmor_runtime.loaded_policy(policy["apparmor"]) == apparmor, "AppArmor changed before start")
            code, stdout, stderr = engine.command(["container", "start", "--attach", "--interactive", cid],
                timeout=args.timeout_seconds, input_bytes=encoded, limit=launcher.OUTPUT_LIMIT, require_success=False)
            child.write(evidence / "container.stdout", stdout)
            child.write(evidence / "container.stderr", stderr)
            after = engine.document(["container", "inspect", cid], singleton=True)
            child.write(evidence / "container-exited.json", child.canonical(after))
            launcher.verify_container(after, status="exited", **kwargs)
            child.require(code == 0 and not stderr, "legacy container failed")
            expected = {"schema": "wikilean.legacy-container-result/v1", "nonce": nonce,
                        "mode": request["mode"], "completed": True}
            child.require(stdout == child.canonical(expected), "private diagnostic channel differs")
            if apparmor is not None:
                child.require(oci_runtime.apparmor_runtime.verify_loaded(policy["apparmor"], args.wheelhouse) == apparmor,
                              "AppArmor changed during run")
            image_after = engine.document(["image", "inspect", selected], singleton=True)
            launcher.verify_engine_image(image_after, image)
            child.require(image_after["Id"] == selected, "actual image changed")
            runtime = {"schema": "wikilean.legacy-outer-oci-observation/v1", "scope": "baseline-diagnostic",
                "authority": False, "offline_replay_verified": False, "baseline_approved": False,
                "runtime": image.runtime(), "config_digest": image.config_digest, "selected_image_id": selected,
                "reference_environment_id": descriptor["environment_id"], "reference_use": "image, dependency and numerical facts only; new runner identity is not old execution authority",
                "engine_id": args.engine_id, "engine_version": args.engine_version, "docker": child.measure(args.docker),
                "socket": str(args.socket), "container_id": cid, "apparmor": apparmor,
                "image_before": image_before, "image_after": image_after,
                "created": before, "exited": after, "request": child.identity(encoded),
                "producer_programs": initial_programs,
                "evidence_files": [{"path": p, **child.measure(path)} for p, path in sorted(child.files(evidence).items())]}
            child.write(evidence / "runtime.json", child.canonical(runtime))
        finally:
            launcher.cleanup_container(engine, cid or name, name)
    result = child.control(evidence / "container-result.json")
    child.require(result["nonce"] == nonce and result["mode"] == request["mode"] and
                  result["authority"] is False and result["baseline_approved"] is False, "container result differs")
    child.require(implementation() == initial_programs, "producer changed")
    for item in runtime["evidence_files"]:
        child.measure(evidence / item["path"], item)
    child.require(child.read(run_root / "host-sentinel") == nonce.encode(), "host sentinel was modified")
    if args.command == "probe":
        output = {"schema": "wikilean.legacy-native-probe/v1", "fixture_only": True, "authority": False,
            "baseline_approved": False, "runtime": {"path": "launch/evidence/runtime.json", **child.identity(child.canonical(runtime))},
            "checks": result["probe"]["checks"], "fixture_bootstrap": result["fixture_bootstrap"], "legacy_stages_executed": False}
        child.write(destination / "probe.json", child.canonical(output))
    else:
        child.verify_preparation(prepared, request["preparation"])
        # Verify the sealed pack again after the actual execution. Output reuse
        # is intentionally not a supported route through this producer.
        pack, _raw, _inventory, _sources, _objects = preparer.loaded_pack(args.manifest, args.pack_root, args.expected_pack_id)
        child.require(result["outputs"] == child.output_records(prepared, record), "measured outputs changed after execution")
        materialize_completed(prepared, destination / "legacy", record, result["outputs"])
        output = {"schema": "wikilean.legacy-baseline-execution/v1", "scope": "baseline-diagnostic",
            "authority": False, "baseline_approved": False, "offline_replay_verified": False,
            **{key: record[key] for key in ("pack", "legacy", "inputs", "absences")},
            "configuration": result["configuration"], "runtime": {"preimage": {"path": "runtime.json", **child.identity(child.canonical(runtime))},
                "support_files": runtime["evidence_files"]},
            "stages": result["stages"], "outputs": result["outputs"], "halo": {**result["halo"],
                "preparation": {"path": "preparation.json", **request["preparation"]}}}
        child.write(destination / "execution.json", child.canonical(output))
    return output


def parser():
    result = argparse.ArgumentParser(description=__doc__)
    commands = result.add_subparsers(dest="command", required=True)
    for name in ("execute", "probe"):
        cmd = commands.add_parser(name)
        for arg in ("destination", "oci-layout", "policy", "wheelhouse", "environment", "docker", "socket"):
            cmd.add_argument("--" + arg, type=Path, required=True)
        for arg in ("expected-environment-id", "docker-sha256", "engine-id", "engine-version"):
            cmd.add_argument("--" + arg, required=True)
        cmd.add_argument("--memory-bytes", type=int, default=6 * 1024**3)
        cmd.add_argument("--stage-timeout-seconds", type=float, default=6 * 3600)
        cmd.add_argument("--timeout-seconds", type=float, default=24 * 3600)
        if name == "execute":
            for arg in ("prepared", "manifest", "pack-root"):
                cmd.add_argument("--" + arg, type=Path, required=True)
            for arg in ("expected-pack-id", "preparation-sha256"):
                cmd.add_argument("--" + arg, required=True)
            cmd.add_argument("--preparation-bytes", type=int, required=True)
    return result


def main(argv=None):
    try:
        value = run(parser().parse_args(argv))
    except (OSError, ValueError, RuntimeError) as exc:
        print(type(exc).__name__ + ": " + str(exc), file=sys.stderr)
        return 1
    print(child.canonical(value).decode(), end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
