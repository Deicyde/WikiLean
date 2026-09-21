#!/usr/bin/env python3
"""Launch a verified Brain replay through an explicitly selected local Docker engine.

No pull, build, registry request, remote daemon, image tag, or evidence-file bypass
exists. The engine image must already be present. This is a trusted-host launcher,
not a signature service: its execution record requires operator review like other
authority evidence. Arbitrary same-user code or a compromised engine is outside
this trust boundary. Direct run_replay_v2 CLI invocations remain fail-closed for OCI.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import platform
import re
import selectors
import signal
import stat
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import execution_environment as environment
import oci_runtime
import run_replay_v2 as runner

LAUNCH_SCHEMA = "wikilean.oci-replay-launch/v1"
CHANNEL_SCHEMA = "wikilean.oci-launch-channel/v1"
BOOTSTRAP = "/opt/wikilean/brain/tools/oci_replay_entrypoint.py"
CHANNEL_LIMIT = 64 * 1024
OUTPUT_LIMIT = 64 * 1024 * 1024
INSPECT_LIMIT = 4 * 1024 * 1024
CID_RE = re.compile(r"^[0-9a-f]{64}$")
LAUNCH_LABEL = "org.wikilean.replay-launch"


class OCILaunchError(RuntimeError):
    """No replay authority follows from this unsuccessful launch."""


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise OCILaunchError(message)


def canonical(value: Any) -> bytes:
    return environment.canonical_json_bytes(value)


def _digest(value: Any) -> str:
    return hashlib.sha256(canonical(value)).hexdigest()


def _real_path(path: Path, *, directory: bool) -> Path:
    _require(path.is_absolute() and str(path) == os.path.normpath(str(path)), "paths must be normalized and absolute")
    _require(path.resolve(strict=True) == path, "paths must not contain symlink components")
    metadata = path.lstat()
    _require(stat.S_ISDIR(metadata.st_mode) if directory else stat.S_ISREG(metadata.st_mode),
             "expected a real directory" if directory else "expected a regular file")
    _require(not any(char in str(path) for char in (",", "\n", "\r", "\x00")), "unsafe Docker mount path")
    return path


def _bounded_process(command: list[str], *, env: dict[str, str], cwd: Path,
                     timeout: float, input_bytes: bytes | None = None,
                     limit: int = INSPECT_LIMIT) -> tuple[int, bytes, bytes]:
    """Bound process output and elapsed time; kill the whole local CLI process group."""
    process = subprocess.Popen(command, cwd=cwd, env=env, stdin=subprocess.PIPE if input_bytes is not None else subprocess.DEVNULL,
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True)
    output = [bytearray(), bytearray()]
    deadline = time.monotonic() + timeout
    try:
        with selectors.DefaultSelector() as selector:
            assert process.stdout is not None and process.stderr is not None
            selector.register(process.stdout, selectors.EVENT_READ, 0)
            selector.register(process.stderr, selectors.EVENT_READ, 1)
            if input_bytes is not None:
                assert process.stdin is not None
                os.set_blocking(process.stdin.fileno(), False)
                selector.register(process.stdin, selectors.EVENT_WRITE, 2)
            pending = memoryview(input_bytes or b"")
            while selector.get_map():
                remaining = deadline - time.monotonic()
                _require(remaining > 0, "local engine command timed out")
                for key, _event in selector.select(min(remaining, 0.25)):
                    if key.data == 2:
                        if pending:
                            written = os.write(key.fd, pending[:65536])
                            pending = pending[written:]
                        if not pending:
                            selector.unregister(key.fileobj)
                            key.fileobj.close()
                    else:
                        chunk = os.read(key.fd, 65536)
                        if chunk:
                            output[key.data].extend(chunk)
                            _require(sum(map(len, output)) <= limit, "local engine output exceeded its byte limit")
                        else:
                            selector.unregister(key.fileobj)
                            key.fileobj.close()
            returncode = process.wait(timeout=max(0.001, deadline - time.monotonic()))
        return returncode, bytes(output[0]), bytes(output[1])
    finally:
        if process.poll() is None:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait(timeout=5)
        for stream in (process.stdin, process.stdout, process.stderr):
            if stream is not None:
                stream.close()


class LocalEngine:
    def __init__(self, executable: Path, expected_sha256: str, socket_path: Path, private_config: Path):
        self.executable = _real_path(executable, directory=False)
        environment._digest(expected_sha256, "Docker executable SHA-256")
        self.expected_sha256 = expected_sha256
        _require(environment.secure_file_digest(executable)[0] == expected_sha256, "Docker executable digest mismatch")
        _require(socket_path.is_absolute() and socket_path.resolve(strict=True) == socket_path,
                 "engine socket must be an explicit real absolute local path")
        metadata = socket_path.lstat()
        _require(stat.S_ISSOCK(metadata.st_mode) and metadata.st_uid in {0, os.getuid()} and
                 not metadata.st_mode & stat.S_IWOTH, "unsafe local Docker socket")
        self.socket_path = socket_path
        self.socket_identity = (metadata.st_dev, metadata.st_ino, metadata.st_uid, metadata.st_mode)
        self.private_config = private_config
        self.prefix = [str(executable), "--config", str(private_config), "--host", "unix://" + str(socket_path)]
        self.environment = {"HOME": str(private_config), "PATH": "/nonexistent", "LANG": "C.UTF-8", "LC_ALL": "C.UTF-8"}

    def command(self, arguments: list[str], *, timeout: float = 30,
                input_bytes: bytes | None = None, limit: int = INSPECT_LIMIT,
                require_success: bool = True) -> tuple[int, bytes, bytes]:
        _require(environment.secure_file_digest(self.executable)[0] == self.expected_sha256, "Docker executable changed")
        metadata = self.socket_path.lstat()
        _require((metadata.st_dev, metadata.st_ino, metadata.st_uid, metadata.st_mode) == self.socket_identity,
                 "local engine socket changed")
        result = _bounded_process([*self.prefix, *arguments], env=self.environment, cwd=self.private_config,
                                  timeout=timeout, input_bytes=input_bytes, limit=limit)
        _require(not require_success or result[0] == 0,
                 "local engine command failed: " + arguments[0])
        return result

    def document(self, arguments: list[str], *, singleton: bool = False) -> dict[str, Any]:
        _code, stdout, stderr = self.command(arguments)
        _require(not stderr, "unexpected local engine diagnostic output")
        try:
            value = json.loads(stdout)
        except (ValueError, UnicodeError) as exc:
            raise OCILaunchError("local engine returned invalid JSON") from exc
        if singleton:
            _require(isinstance(value, list) and len(value) == 1, "engine must identify exactly one object")
            value = value[0]
        _require(isinstance(value, dict), "engine response must be an object")
        return value


def verify_engine_image(observation: dict[str, Any], image: oci_runtime.VerifiedImage) -> None:
    _require(observation.get("Id") == image.config_digest, "engine selected a different image config")
    _require(observation.get("Os") == "linux" and oci_runtime.ARCHITECTURES.get(observation.get("Architecture")) == image.architecture,
             "engine image platform mismatch")
    rootfs = observation.get("RootFS") or {}
    _require(rootfs.get("Type") == "layers" and rootfs.get("Layers") == list(image.diff_ids), "engine image rootfs differs from verified OCI layers")
    config = observation.get("Config") or {}
    _require(not config.get("Volumes"), "engine image has implicit volumes")
    _require((config.get("Labels") or {}).get(oci_runtime.POLICY_LABEL) == image.policy_sha256,
             "engine image numerical policy differs")


def launch_environment(policy: dict[str, Any]) -> dict[str, str]:
    result = runner.base_environment()
    result.update(oci_runtime.numerical_environment(policy))
    return result


def create_arguments(image: oci_runtime.VerifiedImage, workspace: Path, pack_root: Path,
                     policy: dict[str, Any], *, uid: int, gid: int, name: str,
                     memory_bytes: int) -> tuple[list[str], list[str], list[dict[str, Any]]]:
    _require(uid > 0 and gid >= 0, "OCI replay must run as a non-root host user")
    _require(type(memory_bytes) is int and 1024**3 <= memory_bytes <= 128 * 1024**3,
             "memory limit must be between 1 and 128 GiB")
    mounts = [{"Source": str(workspace), "Destination": str(workspace), "RW": False},
              {"Source": str(workspace / "output"), "Destination": str(workspace / "output"), "RW": True},
              {"Source": str(workspace / "scratch"), "Destination": str(workspace / "scratch"), "RW": True},
              {"Source": str(pack_root), "Destination": str(pack_root), "RW": False}]
    # env -i removes all image-default environment values before Python startup.
    child = ["-i", *[f"{key}={value}" for key, value in sorted(launch_environment(policy).items())],
             policy["python"], "-I", "-B", BOOTSTRAP]
    arguments = ["container", "create", "--name", name, "--pull", "never", "--interactive",
                 "--label", LAUNCH_LABEL + "=" + name,
                 "--network", "none", "--read-only", "--cap-drop", "ALL",
                 "--security-opt", "no-new-privileges", "--security-opt", "seccomp=unconfined",
                 "--ipc", "none", "--cgroupns", "private", "--pids-limit", "512",
                 "--tmpfs", "/tmp:rw,nosuid,nodev,noexec,size=64m,mode=1777",
                 "--memory", str(memory_bytes), "--memory-swap", str(memory_bytes),
                 "--user", f"{uid}:{gid}", "--workdir", str(workspace),
                 "--entrypoint", "/usr/bin/env"]
    for mount in mounts:
        options = "type=bind,src=" + mount["Source"] + ",dst=" + mount["Destination"] + ",bind-propagation=rprivate"
        if not mount["RW"]:
            options += ",readonly"
        arguments.extend(["--mount", options])
    arguments.extend([image.config_digest, *child])
    return arguments, child, mounts


def verify_container(value: dict[str, Any], *, image: oci_runtime.VerifiedImage,
                     child: list[str], mounts: list[dict[str, Any]], uid: int, gid: int,
                     memory_bytes: int, status: str, cid: str) -> None:
    _require(value.get("Id") == cid and value.get("Image") == image.config_digest, "created container identity mismatch")
    config = value.get("Config") or {}
    _require(config.get("Image") == image.config_digest and config.get("User") == f"{uid}:{gid}" and
             config.get("Entrypoint") == ["/usr/bin/env"] and config.get("Cmd") == child and
             config.get("WorkingDir") == mounts[0]["Destination"] and config.get("OpenStdin") is True and
             config.get("Tty") is False, "created container command differs from the sealed launch")
    host = value.get("HostConfig") or {}
    _require(host.get("NetworkMode") == "none" and host.get("ReadonlyRootfs") is True and
             host.get("Privileged") is False and host.get("CapDrop") == ["ALL"] and not host.get("CapAdd") and
             set(host.get("SecurityOpt") or []) == {"no-new-privileges", "seccomp=unconfined"} and
             host.get("IpcMode") == "none" and host.get("CgroupnsMode") == "private" and
             not host.get("PidMode") and host.get("PidsLimit") == 512 and
             host.get("Memory") == memory_bytes and host.get("MemorySwap") == memory_bytes and
             host.get("Tmpfs") == {"/tmp": "rw,nosuid,nodev,noexec,size=64m,mode=1777"} and
             not host.get("Devices") and not host.get("DeviceRequests") and not host.get("Binds") and
             not host.get("VolumesFrom") and not host.get("Links"), "created container isolation policy mismatch")
    observed_mounts = value.get("Mounts")
    _require(isinstance(observed_mounts, list) and len(observed_mounts) == len(mounts), "created container has unexpected mounts")
    projected = []
    for item in observed_mounts:
        _require(item.get("Type") == "bind" and item.get("Propagation") == "rprivate", "unexpected container mount type/propagation")
        projected.append({key: item.get(key) for key in ("Source", "Destination", "RW")})
    _require(sorted(projected, key=lambda item: item["Destination"]) == sorted(mounts, key=lambda item: item["Destination"]),
             "created container mount closure mismatch")
    state = value.get("State") or {}
    _require(state.get("Status") == status and state.get("Running") is False and not state.get("OOMKilled"),
             "container state is not the expected stable state")
    if status == "exited":
        _require(state.get("ExitCode") == 0 and not state.get("Error"), "container replay failed")


def cleanup_container(engine: LocalEngine, target: str, name: str) -> None:
    """Reconcile uncertain create outcomes; remove only this invocation's object."""
    status, stdout, stderr = engine.command(["container", "inspect", target], require_success=False)
    if status != 0:
        _require(not stdout and (b"No such container" in stderr or b"No such object" in stderr),
                 "cannot reconcile container cleanup; inspect launch " + name)
        return
    value = json.loads(stdout)
    _require(isinstance(value, list) and len(value) == 1 and isinstance(value[0], dict), "ambiguous cleanup target")
    observed = value[0]
    cid = observed.get("Id")
    _require(isinstance(cid, str) and CID_RE.fullmatch(cid) is not None and
             observed.get("Name") == "/" + name and
             ((observed.get("Config") or {}).get("Labels") or {}).get(LAUNCH_LABEL) == name,
             "refusing to remove a container not owned by this launch")
    if CID_RE.fullmatch(target):
        _require(cid == target, "cleanup container ID mismatch")
    engine.command(["container", "rm", "--force", cid])


def run(args: argparse.Namespace) -> dict[str, Any]:
    runner.require_isolated_startup()
    runner._validated_stage_timeout_seconds(args.stage_timeout_seconds)
    _require(math.isfinite(args.timeout_seconds) and 0 < args.timeout_seconds <= 7 * 24 * 60 * 60,
             "launch timeout must be finite, positive, and at most seven days")
    _require(sys.platform.startswith("linux"), "trusted OCI replay requires a native Linux host with a local engine; this host is unsupported")
    _require(sys.version_info[:2] == (3, 12), "OCI launcher requires CPython 3.12")
    workspace = _real_path(args.context.parent, directory=True)
    manifest = _real_path(args.manifest, directory=False)
    pack_root = _real_path(args.root or manifest.parent, directory=True)
    _require(manifest.is_relative_to(pack_root), "manifest is outside the sealed pack root")
    _require(not (workspace == pack_root or workspace in pack_root.parents or pack_root in workspace.parents),
             "pack and workspace must be disjoint")
    _require(args.context == workspace / "build-context.json", "context must be the prepared workspace context")
    policy, raw_policy = oci_runtime.read_control(args.policy)
    _require(raw_policy == canonical(policy), "runtime policy must be canonical JSON")
    context = runner.build_context.BuildContext.load(args.context)
    descriptor = runner._verify_execution_environment(workspace, context)
    document, _raw = runner.contracts.load_canonical_json(manifest)
    pack = runner.contracts.validate_offline_pack(document)
    _require(pack["schema"] in {runner.contracts.PACK_SCHEMA_V2, runner.contracts.PACK_SCHEMA_V3}, "OCI replay requires pack v2 or v3")
    runner.contracts.verify_offline_pack_files(pack, pack_root, manifest_path=manifest)
    _require(pack["offline_pack_id"] == context.replay.offline_pack_id, "prepared context belongs to another pack")
    reducer_files = tuple((item["logical_path"], item["bytes"], item["sha256"]) for item in pack["reducer"]["files"])
    runner._validate_workspace(args.context, context, reducer_files)
    image = oci_runtime.verify_image(args.oci_layout, descriptor["runtime"]["manifest_digest"], policy,
                                     args.wheelhouse, descriptor)
    _require(platform.machine().lower() == image.architecture, "emulated or cross-architecture replay is forbidden")
    _require(args.receipt.is_absolute() and not args.receipt.exists(), "launch receipt must be a fresh absolute path")
    _real_path(args.receipt.parent, directory=True)
    receipt_parent = args.receipt.parent.stat()
    _require(receipt_parent.st_uid == os.getuid() and not stat.S_IMODE(receipt_parent.st_mode) & 0o077,
             "receipt parent must be private to the current user")
    _require(not args.receipt.is_relative_to(workspace) and not args.receipt.is_relative_to(pack_root),
             "launch receipt must be outside the pack and replay workspace")
    nonce = uuid.uuid4().hex
    payload = {"schema": CHANNEL_SCHEMA, "nonce": nonce, "runtime": image.runtime(), "policy": policy,
               "runner_arguments": ["--manifest", str(manifest), "--root", str(pack_root), "--context", str(args.context),
                                    "--expected-generation-id", context.generation_id, "--python", policy["python"],
                                    "--stage-timeout-seconds", str(args.stage_timeout_seconds)]}
    encoded = canonical(payload)
    _require(len(encoded) <= CHANNEL_LIMIT, "launch channel exceeded limit")
    uid, gid = os.getuid(), os.getgid()
    with tempfile.TemporaryDirectory(prefix="wikilean-oci-engine-") as temporary:
        private = Path(temporary).resolve()
        (private / "config.json").write_bytes(b"{}")
        engine = LocalEngine(args.docker, args.docker_sha256, args.socket, private)
        info = engine.document(["info", "--format", "{{json .}}"])
        _require(info.get("ID") == args.engine_id and info.get("ServerVersion") == args.engine_version and
                 info.get("OSType") == "linux", "local engine identity/version differs from the approved launch")
        inspected_image = engine.document(["image", "inspect", image.config_digest], singleton=True)
        verify_engine_image(inspected_image, image)
        name = "wikilean-replay-" + nonce
        command, child, mounts = create_arguments(image, workspace, pack_root, policy, uid=uid, gid=gid,
                                                  name=name, memory_bytes=args.memory_bytes)
        cid = None
        cleaned = False
        try:
            _code, stdout, stderr = engine.command(command)
            _require(not stderr, "unexpected container-create diagnostics")
            cid = stdout.decode("ascii").strip()
            _require(CID_RE.fullmatch(cid) is not None, "engine did not return one complete container ID")
            before = engine.document(["container", "inspect", cid], singleton=True)
            verify_container(before, image=image, child=child, mounts=mounts, uid=uid, gid=gid,
                             memory_bytes=args.memory_bytes, status="created", cid=cid)
            _code, stdout, stderr = engine.command(["container", "start", "--attach", "--interactive", cid],
                                                   timeout=args.timeout_seconds, input_bytes=encoded, limit=OUTPUT_LIMIT)
            after = engine.document(["container", "inspect", cid], singleton=True)
            verify_container(after, image=image, child=child, mounts=mounts, uid=uid, gid=gid,
                             memory_bytes=args.memory_bytes, status="exited", cid=cid)
            verify_engine_image(engine.document(["image", "inspect", image.config_digest], singleton=True), image)
            lines = stdout.splitlines()
            _require(bool(lines), "replay did not return a launch-channel result")
            result = oci_runtime._json(lines[-1], "launch-channel result")
            _require(result == {"schema": CHANNEL_SCHEMA, "nonce": nonce, "ok": True}, "launch-channel result mismatch")
            cleanup_container(engine, cid, name)
            cleaned = True
            receipt = {"schema": LAUNCH_SCHEMA, "profile": "trusted-local-engine", "ok": True,
                       "runtime": image.runtime(), "config_digest": image.config_digest,
                       "environment_id": descriptor["environment_id"], "offline_pack_id": pack["offline_pack_id"],
                       "generation_id": context.generation_id, "policy_sha256": image.policy_sha256,
                       "engine_id": args.engine_id, "engine_version": args.engine_version,
                       "docker_sha256": args.docker_sha256, "container_id": cid,
                       "create_observation_sha256": _digest(before), "exit_observation_sha256": _digest(after),
                       "observations": {"created": before, "exited": after},
                       "request_sha256": hashlib.sha256(encoded).hexdigest(),
                       "stdout_sha256": hashlib.sha256(stdout).hexdigest(), "stderr_sha256": hashlib.sha256(stderr).hexdigest()}
            fd = os.open(args.receipt, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o400)
            with os.fdopen(fd, "wb") as stream:
                stream.write(canonical(receipt))
                stream.flush()
                os.fsync(stream.fileno())
            parent_fd = os.open(args.receipt.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            try:
                os.fsync(parent_fd)
            finally:
                os.close(parent_fd)
            return receipt
        finally:
            if not cleaned:
                cleanup_container(engine, cid if cid is not None and CID_RE.fullmatch(cid) else name, name)


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    for name in ("manifest", "context", "oci-layout", "policy", "wheelhouse", "docker", "socket", "receipt"):
        result.add_argument("--" + name, type=Path, required=True)
    result.add_argument("--root", type=Path)
    for name in ("docker-sha256", "engine-id", "engine-version"):
        result.add_argument("--" + name, required=True)
    result.add_argument("--stage-timeout-seconds", type=float, default=runner.DEFAULT_STAGE_TIMEOUT_SECONDS)
    result.add_argument("--timeout-seconds", type=float, default=12 * 60 * 60)
    result.add_argument("--memory-bytes", type=int, default=16 * 1024**3)
    return result


def main(argv: list[str] | None = None) -> int:
    try:
        value = run(parser().parse_args(argv))
    except (OSError, ValueError, RuntimeError) as exc:
        print(canonical({"ok": False, "error": {"type": type(exc).__name__, "message": str(exc)}}).decode(), file=sys.stderr)
        return 1
    print(canonical(value).decode())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
