#!/usr/bin/env python3
"""Private native-container supervisor for the diagnostic old reducer.

Only run_legacy_baseline.py publishes an execution record after its own engine
inspection. This entry point's result is an unsigned intermediate observation,
never replay authority or baseline approval. Same-user hostile host code and a
compromised local engine are outside the operator-reviewed trust boundary.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
from pathlib import Path, PurePosixPath
import selectors
import signal
import socket
import stat
import subprocess
import sys
import time
import types

LEGACY_COMMIT = "ebac34dc1d07b66ce97692c31a914a084328f5df"
LEGACY_TREE = "753f8e002466ec27d75cd6c41360e1a930dc178b"
PROGRAM_HASHES = {
    "brain/build_snapshot.py": "9cf38da56adcedbaba4aa85013e5f3a3864a6c560cda8a54f5f1247fc457e657",
    "brain/build_common.py": "f72b8b51b97c88534fb97f6d82a4e64ad829598d29ed6fbacf5e631e07eb608e",
    "brain/store.py": "52a077570554f22eaaabd65683668f9014170a299f82a464241c9008a4bc9d84",
    "brain/build_shards.py": "97245641c611df951013361ca9ff9c3d68d4061138c37c583c6e84f641915c4e",
    "brain/build_cells.py": "699c77aa4c702e4a6e58f1e8c3824d9638a434a2da45f93711a735db41f4ef54",
    "brain/layout.py": "acaf58f767d55fcd917ba16d1c120132ea22686e5971c7f9b4005c64fa8fbf7a",
    "brain/build_frontier.py": "32237eba087f9b559c0d7be2d835cdd363de13cf294f7e458c4425c2cb7f780d",
    "brain/frontier_suitability.py": "5b39d2c1dce70c93cdc37cf387c9eb3591191c7ac223b2fd03faeb6e29e90da4",
    "brain/build_cell_shards.py": "e81214927487455d3992dfc15ee9e802dca30c946eb22537683622c8485ea56c",
    "site/build_brain_page.py": "6542d171af47304a0ace0d50c64651968156e0363acd96bc3c4d5091bd0e846f",
}
HALO_SHA = "f83757dc8db0675d651dacd9d5625a9d1bf25b2ca141de4965be5039a52a037e"
STAGES = (("brain/build_snapshot.py", ()), ("brain/build_shards.py", ()),
          ("brain/build_cells.py", ("--attach", "generalization,special_case")),
          ("brain/build_snapshot.py", ("--from-jsonl",)),
          ("brain/build_frontier.py", ()), ("brain/build_cell_shards.py", ()),
          ("site/build_brain_page.py", ()))
OUTPUT_DIRS = ("brain/data", "site/assets", "site/out", "manage/data")
SEMANTIC = {"brain/data/" + name for name in (
    "nodes.jsonl", "edges.jsonl", "edges_links.jsonl", "cells.jsonl", "synapses.jsonl",
    "frontier.jsonl", "frontier_graph.json", "brain.sqlite3")}
MAX_FILE = 8 * 1024**3
MAX_CONTROL = 32 * 1024**2
MTIME_NS = 1788825600000000000


def require(value, message):
    if not value:
        raise ValueError(message)


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def identity(raw):
    return {"sha256": hashlib.sha256(raw).hexdigest(), "bytes": len(raw)}


def literal(value):
    require(isinstance(value, str) and value and not value.startswith("/") and
            all(p not in {"", ".", ".."} for p in value.split("/")) and
            not any(c in value for c in "\\\x00\r\n"), "unsafe literal path")
    return value


def real(path, directory=True):
    path = Path(path)
    require(path.is_absolute() and path.resolve(strict=True) == path, "path must be absolute without symlinks")
    require(stat.S_ISDIR(path.lstat().st_mode) if directory else stat.S_ISREG(path.lstat().st_mode), "path type differs")
    return path


def measure(path, expected=None, *, copy_to=None, maximum=MAX_FILE):
    path = Path(path)
    real(path.parent)
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    destination = None
    try:
        before = os.fstat(fd)
        require(stat.S_ISREG(before.st_mode) and before.st_size <= maximum, "file is not bounded and regular")
        if copy_to is not None:
            copy_to.parent.mkdir(parents=True, exist_ok=True)
            destination = copy_to.open("xb")
        digest, size = hashlib.sha256(), 0
        while chunk := os.read(fd, 1024 * 1024):
            size += len(chunk)
            require(size <= maximum, "file grew past limit")
            digest.update(chunk)
            if destination is not None:
                destination.write(chunk)
        after = os.fstat(fd)
        require((before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns, before.st_ctime_ns) ==
                (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns), "file changed during read")
        result = {"sha256": digest.hexdigest(), "bytes": size}
        if expected is not None:
            require(result == {key: expected[key] for key in result}, "file identity differs: " + str(path))
        return result
    finally:
        os.close(fd)
        if destination is not None:
            destination.flush(); os.fsync(destination.fileno()); destination.close()


def read(path, expected=None, maximum=MAX_CONTROL):
    path = Path(path)
    real(path.parent)
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as stream:
        before = os.fstat(stream.fileno())
        require(stat.S_ISREG(before.st_mode) and before.st_size <= maximum, "file is not bounded and regular")
        raw = stream.read(maximum + 1)
        after = os.fstat(stream.fileno())
    require(len(raw) <= maximum and
            (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns, before.st_ctime_ns) ==
            (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns), "captured bytes changed")
    if expected is not None:
        require(identity(raw) == {key: expected[key] for key in ("sha256", "bytes")}, "file identity differs: " + str(path))
    return raw


def control(path, expected=None):
    raw = read(path, expected)
    value = json.loads(raw)
    require(raw == canonical(value), "control must be canonical JSON")
    return value


def write(path, raw):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("xb") as stream:
        stream.write(raw); stream.flush(); os.fsync(stream.fileno())
    return {"path": path.name, **identity(raw)}


def files(root):
    real(root)
    found = {}
    for parent, directories, names in os.walk(root, followlinks=False):
        for name in directories + names:
            path = Path(parent) / name
            mode = path.lstat().st_mode
            require(stat.S_ISREG(mode) or stat.S_ISDIR(mode), "non-regular tree member")
            if stat.S_ISREG(mode):
                found[path.relative_to(root).as_posix()] = path
                require(len(found) <= 500000, "tree exceeds member bound")
    return found


def check_absences(root, absences):
    for item in absences:
        if "path" in item:
            require(not os.path.lexists(root / literal(item["path"])), "declared absent input is present")
        else:
            pattern = item["path_pattern"]
            literal(pattern)
            require(not list(root.glob(pattern)), "declared absent pattern has members")


def verify_preparation(root, expected, *, cold=False):
    root = real(root)
    record = control(root / "preparation.json", expected)
    require(record.get("schema") == "wikilean.legacy-baseline-preparation/v1" and
            record.get("scope") == "baseline-diagnostic" and record.get("authority") is False and
            record.get("baseline_approved") is False and record.get("executed") is False, "unexpected preparation scope")
    legacy = record["legacy"]
    require(legacy["git_commit"] == LEGACY_COMMIT and legacy["git_tree"] == LEGACY_TREE, "wrong legacy generation")
    programs = {literal(p["path"]): p for p in legacy["program_files"]}
    require(len(programs) == len(legacy["program_files"]) and set(programs) == set(PROGRAM_HASHES), "legacy program closure differs")
    inputs = {literal(p["path"]): p for p in record["inputs"]}
    require(len(inputs) == len(record["inputs"]) and not set(inputs) & set(programs), "input ownership differs")
    require(record["output_directories"] == list(OUTPUT_DIRS), "output mounts differ")
    require(not any(p.startswith(d + "/") for p in inputs for d in OUTPUT_DIRS if d != "brain/data"),
            "preparer provides no readonly overlay for this mixed input directory")
    config = record["configuration"]
    require(config["attach"] == ["generalization", "special_case"] and config["layout_enabled"] is True and
            config["layout_iterations"] == 200 and type(config["external_node_cap"]) is int and
            config["external_node_cap"] >= 0 and config["staging_mtime_epoch_seconds"] * 10**9 == MTIME_NS,
            "old stage configuration differs")
    require(set(files(root / "code")) == set(programs) | set(inputs), "prepared code/input closure differs")
    for path, row in {**programs, **inputs}.items():
        if path in programs:
            require(row["sha256"] == PROGRAM_HASHES[path], "legacy imported program changed")
        measure(root / "code" / path, row)
        require((root / "code" / path).stat().st_mtime_ns == MTIME_NS, "staged input/program mtime differs")
    overlays = {p: row for p, row in inputs.items() if p.startswith("brain/data/")}
    require(set(files(root / "input")) == set(overlays), "mixed input overlay closure differs")
    for path, row in overlays.items():
        measure(root / "input" / path, row)
        require((root / "input" / path).stat().st_mtime_ns == MTIME_NS, "overlay mtime differs")
    require(set(files(root / "support")) == {"manage/halo.py"} and record["halo_program"]["sha256"] == HALO_SHA,
            "halo support closure differs")
    measure(root / record["halo_program"]["path"], record["halo_program"])
    check_absences(root / "code", record["absences"])
    if cold:
        require(not files(root / "output") and not files(root / "scratch"), "legacy execution requires a cold output/scratch")
    return record


def stage_environment(root, config, runtime_environment):
    result = dict(runtime_environment)
    result.update(BRAIN_EXTERNAL_DIR=str(root / "code/inputs/external"),
                  BRAIN_EXT_NODE_CAP=str(config["external_node_cap"]),
                  BRAIN_MATHLIB_CHECKOUT=str(root / "code/inputs/mathlib/Mathlib"),
                  BRAIN_DECL_ORACLE=str(root / "code/inputs/decl_oracle/declaration-data.json"),
                  TMPDIR=str(root / "scratch"))
    return result


def boundary(root, record, runtime_mounts):
    code = root / "code"
    prefix = ["/usr/bin/bwrap", "--die-with-parent", "--new-session", "--unshare-all", "--cap-drop", "ALL",
              "--dev", "/dev", "--proc", "/proc", "--tmpfs", "/tmp"]
    for source, destination in runtime_mounts:
        prefix += ["--ro-bind", str(source), str(destination)]
    prefix += ["--ro-bind", str(code), str(code)]
    for relative in OUTPUT_DIRS:
        prefix += ["--bind", str(root / "output" / relative), str(code / relative)]
    for row in record["inputs"]:
        if row["path"].startswith("brain/data/"):
            prefix += ["--ro-bind", str(root / "input" / row["path"]), str(code / row["path"])]
    prefix += ["--bind", str(root / "scratch"), str(root / "scratch"),
               "--remount-ro", "/", "--chdir", str(code), "--"]
    return prefix


def execute(command, cwd, environment, evidence, name, *, timeout=21600, limit=64 * 1024**2):
    """Retain bounded real logs on success, nonzero exit, timeout or interruption."""
    require(not isinstance(timeout, bool) and math.isfinite(timeout) and 0 < timeout <= 7 * 86400, "invalid stage deadline")
    require(type(limit) is int and 0 < limit <= 64 * 1024**2, "invalid log limit")
    handles = [(evidence / (name + "." + kind)).open("xb") for kind in ("stdout", "stderr")]
    process = None
    counts = [0, 0]
    try:
        process = subprocess.Popen(command, cwd=cwd, env=dict(environment), stdin=subprocess.DEVNULL,
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True)
        deadline = time.monotonic() + timeout
        with selectors.DefaultSelector() as selector:
            for i, stream in enumerate((process.stdout, process.stderr)):
                selector.register(stream, selectors.EVENT_READ, i)
            while selector.get_map():
                require(time.monotonic() < deadline, "legacy subprocess timed out")
                for key, _ in selector.select(min(0.2, max(0, deadline - time.monotonic()))):
                    chunk = os.read(key.fd, min(65536, limit - counts[key.data] + 1))
                    if not chunk:
                        selector.unregister(key.fileobj)
                    else:
                        handles[key.data].write(chunk[:max(0, limit - counts[key.data])])
                        counts[key.data] += len(chunk)
                        require(counts[key.data] <= limit, "legacy subprocess log exceeded limit")
            status = process.wait(timeout=max(0.001, deadline - time.monotonic()))
    finally:
        if process is not None:
            if process.poll() is None:
                os.killpg(process.pid, signal.SIGKILL); process.wait(timeout=5)
            for stream in (process.stdout, process.stderr):
                stream.close()
        for stream in handles:
            stream.flush(); os.fsync(stream.fileno()); stream.close()
    return {"exit": status, **{kind: {"path": name + "." + kind,
             **measure(evidence / (name + "." + kind))} for kind in ("stdout", "stderr")}}


PROBE = '''import json,os,socket,sys
v=json.loads(sys.argv[1]); outcomes={}
def denied(name,fn):
 try: fn()
 except OSError: outcomes[name]=True
 else: raise RuntimeError("kernel allowed "+name)
for i,p in enumerate(v["readonly"]):
 denied("readonly-"+str(i),lambda p=p:os.open(p,os.O_WRONLY))
denied("root-write",lambda:os.open("/undeclared-write",os.O_WRONLY|os.O_CREAT,0o600))
for i,p in enumerate(v["outside"]):
 denied("outside-read-"+str(i),lambda p=p:os.open(p,os.O_RDONLY))
for i,p in enumerate(v["writable"]):
 with open(p,"xb") as f:f.write(b"probe")
 with open(p,"rb") as f:assert f.read()==b"probe"
 os.unlink(p);outcomes["writable-"+str(i)]=True
s=socket.socket();s.settimeout(1)
denied("outer-loopback",lambda:s.connect(("127.0.0.1",v["port"])));s.close()
s=socket.socket(socket.AF_INET,socket.SOCK_DGRAM)
denied("external-network",lambda:s.sendto(b"probe",("198.18.0.1",9)));s.close()
print(json.dumps(outcomes,sort_keys=True,separators=(",",":")))
'''


def kernel_probe(root, record, prefix, python, environment, evidence, sentinel):
    code_paths = sorted(files(root / "code"))
    readonly = [str(root / "code" / next(p for p in code_paths if not p.startswith("brain/data/")))]
    readonly += [str(root / "code" / row["path"]) for row in record["inputs"] if row["path"].startswith("brain/data/")][:1]
    options = {"readonly": readonly, "outside": [str(sentinel), "/tmp" + str(sentinel)],
               "writable": [str(root / "code" / p / ".kernel-probe") for p in OUTPUT_DIRS] +
                           [str(root / "scratch/.kernel-probe")]}
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0)); listener.listen(1)
        options["port"] = listener.getsockname()[1]
        result = execute([*prefix, str(python), "-I", "-B", "-c", PROBE, canonical(options).decode()],
                         root, environment, evidence, "kernel-probe", timeout=30)
    require(result["exit"] == 0 and not result["stderr"]["bytes"], "actual strict kernel probe failed")
    outcomes = json.loads(read(evidence / result["stdout"]["path"]))
    expected = {"root-write", "outer-loopback", "external-network"} | {
        key + str(i) for key, values in (("readonly-", readonly), ("outside-read-", options["outside"]),
                                        ("writable-", options["writable"])) for i in range(len(values))}
    require(set(outcomes) == expected and all(v is True for v in outcomes.values()), "kernel denial proof is incomplete")
    return {"schema": "wikilean.legacy-kernel-probe/v1", "fixture_only": True, "authority": False,
            "baseline_approved": False, "checks": outcomes, "process": result,
            "program": identity(PROBE.encode()), "arguments": options,
            "bubblewrap": measure(Path("/usr/bin/bwrap"))}


def output_records(root, record):
    inputs = {p["path"]: p for p in record["inputs"]}
    rows = []
    for relative, path in sorted(files(root / "output").items()):
        if relative in inputs:
            # bwrap creates empty mountpoint placeholders in the host output.
            require(path.stat().st_size == 0, "hidden mixed-input mountpoint acquired bytes")
            continue
        require(any(relative.startswith(d + "/") for d in OUTPUT_DIRS), "undeclared output directory")
        rows.append({"path": relative, **measure(path)})
    require(SEMANTIC <= {row["path"] for row in rows}, "legacy stage output is incomplete")
    return rows


def run_stages(root, record, prefix, python, environment, evidence, python_command, halo, *, timeout):
    stages = []
    halo_record = None
    for i, (program, arguments) in enumerate(STAGES):
        command = python_command(python, root / "code" / program, *arguments)
        result = execute([*prefix, *command], root / "code", environment, evidence, f"stage-{i + 1}", timeout=timeout)
        stages.append({"program": program, "argv": list(command), **result})
        write(evidence / f"stage-{i + 1}.json", canonical(stages[-1]))
        require(result["exit"] == 0, "legacy stage failed: " + program)
        if i == 3:
            program_raw = read(root / "support/manage/halo.py", maximum=1024**2)
            cells = read(root / "output/brain/data/cells.jsonl", maximum=halo.MAX_INPUT_BYTES)
            synapses = read(root / "output/brain/data/synapses.jsonl", maximum=halo.MAX_INPUT_BYTES)
            raw, report = halo.project(program_raw, cells, synapses)
            write(root / "output/manage/data/halo.json", raw)
            write(evidence / "halo-program.py", program_raw)
            write(evidence / "halo-report.json", canonical(report))
            halo_record = {"output": {"path": "manage/data/halo.json", **identity(raw)},
                           "program": {"path": "halo-program.py", **identity(program_raw)},
                           "report": {"path": "halo-report.json", **identity(canonical(report))}}
    return stages, halo_record


def main():
    require(sys.platform.startswith("linux") and sys.flags.isolated and sys.version_info[:2] == (3, 12),
            "native isolated CPython 3.12 is required")
    request = json.loads(sys.stdin.buffer.read(MAX_CONTROL + 1))
    require(request["schema"] == "wikilean.legacy-container-request/v1" and request["mode"] in {"legacy-stages", "kernel-probe"},
            "unknown diagnostic request")
    root, run = real(Path(request["prepared"])), real(Path(request["run"]))
    evidence = real(run / "evidence")
    measure(Path(__file__).resolve(), request["executor"])
    sys.path.insert(0, "/opt/wikilean/brain/tools")
    import run_replay_v2 as runner
    import oci_runtime
    import execution_environment as environment_contract
    policy = oci_runtime.validate_policy(request["policy"])
    oci_runtime.verify_numerical_runtime(policy)
    python = Path(sys.executable).resolve(strict=True)
    facts = environment_contract.probe_python_runtime(executable_path=python)
    require(facts == request["expected_python"], "live Python differs from retained image descriptor")
    if request["mode"] == "legacy-stages":
        record = verify_preparation(root, request["preparation"], cold=True)
    else:
        record = request["probe_preparation"]
        require(not files(root / "output") and not files(root / "scratch"), "probe requires fresh output")
    child_environment = stage_environment(root, record["configuration"],
        {**runner._environment(python), **oci_runtime.numerical_environment(policy)})
    prefix = boundary(root, record, runner._linux_runtime_mounts(runner._runtime_roots(python)))
    config = {"settings": record["configuration"], "environment": child_environment, "sandbox_argv": prefix,
              "stages": [{"program": p, "arguments": list(a)} for p, a in STAGES]}
    write(evidence / "configuration.json", canonical(config))
    probe = kernel_probe(root, record, prefix, python, child_environment, evidence, run / "host-sentinel")
    write(evidence / "kernel-probe.json", canonical(probe))
    result = {"schema": "wikilean.legacy-container-result/v1", "authority": False, "baseline_approved": False,
              "mode": request["mode"], "nonce": request["nonce"], "python": facts, "probe": probe,
              "configuration": {"preimage": {"path": "configuration.json", **identity(canonical(config))}}}
    if request["mode"] == "legacy-stages":
        halo_raw = read(run / "tool/legacy_halo_projection.py", request["halo_projector"])
        halo = types.ModuleType("captured_legacy_halo")
        exec(compile(halo_raw, "legacy_halo_projection.py", "exec"), halo.__dict__)
        stages, halo_record = run_stages(root, record, prefix, python, child_environment, evidence,
                                       runner._python_command, halo, timeout=request["stage_timeout"])
        verify_preparation(root, request["preparation"])
        result.update(stages=stages, halo=halo_record, outputs=output_records(root, record))
    else:
        command = runner._python_command(python, root / "code/brain/probe.py")
        bootstrap = execute([*prefix, *command], root / "code", child_environment, evidence, "fixture-bootstrap", timeout=30)
        require(bootstrap["exit"] == 0 and not bootstrap["stderr"]["bytes"], "synthetic native stage bootstrap failed")
        observed = json.loads(read(evidence / bootstrap["stdout"]["path"]))
        require(observed == {"helper": "captured-helper", "numpy": policy["numpy"]["version"], "sum": 10.0,
                             "external_node_cap": "32000", "fixture_only": True}, "synthetic native child result differs")
        result["fixture_bootstrap"] = {"argv": list(command), **bootstrap, "observed": observed}
    write(evidence / "container-result.json", canonical(result))
    print(canonical({"schema": result["schema"], "nonce": request["nonce"], "mode": request["mode"], "completed": True}).decode(), end="")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(type(exc).__name__ + ": " + str(exc), file=sys.stderr)
        raise SystemExit(1)
