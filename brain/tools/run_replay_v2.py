#!/usr/bin/env python3
"""Execute every stage of one freshly prepared Brain replay workspace.

The pack verifier/materializer is responsible for constructing the workspace.
This runner validates that boundary, executes the sealed stage schedule exactly
once and in order, and requires an operating-system sandbox for every reducer
subprocess.  It has no flag that disables isolation.
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
import shutil
import stat
import subprocess
import sys
import sysconfig
import time
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from types import MappingProxyType
from typing import Any


HERE = Path(__file__).resolve().parent
BRAIN = HERE.parent
for module_root in (BRAIN, HERE):
    if str(module_root) not in sys.path:
        sys.path.insert(0, str(module_root))

import build_context  # noqa: E402
import authority_contracts as contracts  # noqa: E402
import execution_environment as execution_env  # noqa: E402


class ReplayExecutionError(RuntimeError):
    """A prepared replay cannot be executed safely or completely."""


@dataclass(frozen=True, slots=True)
class IsolationBoundary:
    name: str
    prefix: tuple[str, ...]
    executable: Path | None = None
    policy: Mapping[str, Any] | None = None


@dataclass(frozen=True, slots=True)
class ReplayResult:
    generation_id: str
    isolation: str
    stages: tuple[str, ...]

    def to_document(self) -> dict[str, Any]:
        return {
            "generation_id": self.generation_id,
            "isolation": self.isolation,
            "ok": True,
            "stages": list(self.stages),
        }


Executor = Callable[
    [tuple[str, ...], Path, Mapping[str, str], IsolationBoundary], int
]
ProbeExecutor = Callable[
    [tuple[str, ...], Path, Mapping[str, str], IsolationBoundary],
    tuple[int, bytes, bytes],
]
SandboxProbe = Callable[[IsolationBoundary], dict[str, Any]]
ReducerFileSpec = tuple[str, int, str]
OutputState = tuple[int, int, int, int, int, int, str | None]
EXECUTION_ENVIRONMENT_NAME = "execution-environment.json"
PROBE_PROGRAM = HERE / "probe_execution_environment.py"
SANDBOX_POLICY_ID = "brain-replay-v1"
PROBE_STDOUT_LIMIT = 1024 * 1024
PROBE_STDERR_LIMIT = 64 * 1024
PROBE_TIMEOUT_SECONDS = 30.0
SANDBOX_VERSION_TIMEOUT_SECONDS = 5.0
DEFAULT_STAGE_TIMEOUT_SECONDS = 6 * 60 * 60.0
RUNNER_FILES = MappingProxyType({
    "brain/build_context.py": BRAIN / "build_context.py",
    "brain/tools/authority_contracts.py": HERE / "authority_contracts.py",
    "brain/tools/execution_environment.py": HERE / "execution_environment.py",
    "brain/tools/prepare_replay_v2.py": HERE / "prepare_replay_v2.py",
    "brain/tools/probe_execution_environment.py": PROBE_PROGRAM,
    "brain/tools/run_offline.py": HERE / "run_offline.py",
    "brain/tools/run_replay_v2.py": Path(__file__).resolve(),
})

_BUBBLEWRAP_VERSION_RE = re.compile(
    r"^(?:bubblewrap|bwrap) ([0-9](?:[0-9A-Za-z._+!-]*[0-9A-Za-z])?)$"
)

_RUN_STAGE = (
    "import os,runpy,sys;"
    "program=sys.argv.pop(1);"
    "sys.path.insert(0,os.path.dirname(program));"
    "sys.argv[0]=program;"
    "runpy.run_path(program,run_name='__main__')"
)


def _sandbox_policy_document(backend: str) -> dict[str, Any]:
    common: dict[str, Any] = {
        "schema": "wikilean.replay-sandbox-policy/v1",
        "backend": backend,
        "default": "deny",
        "network": "disabled",
        "read": ["prepared-workspace", "python-runtime"],
        "write": ["output", "scratch"],
    }
    if backend == "darwin-sandbox-exec":
        common.update(
            {
                "platform_baseline": "system.sb",
                "process_exec": "interpreter-only",
                "process_fork": "denied",
            }
        )
    elif backend == "linux-bubblewrap":
        common.update(
            {
                "capabilities": "none",
                "devices": "minimal",
                "namespaces": "all-unshared",
                "proc": "isolated",
                "temporary_directory": "ephemeral",
            }
        )
    else:
        raise ReplayExecutionError(f"unknown replay sandbox backend {backend!r}")
    return common


def _host_operating_system() -> str:
    if sys.platform == "darwin":
        return "darwin"
    if sys.platform.startswith("linux"):
        return "linux"
    raise ReplayExecutionError(
        f"no supported replay runtime for platform {sys.platform!r}"
    )


def _development_runtime_facts() -> dict[str, Any]:
    operating_system = _host_operating_system()
    architecture = platform.machine().lower()
    libc_name, libc_version = platform.libc_ver()
    return {
        "kind": "development-host",
        "os": operating_system,
        "architecture": architecture,
        "host_fingerprint": execution_env.development_host_fingerprint(
            operating_system=operating_system,
            architecture=architecture,
            kernel_release=platform.release(),
            libc_name=libc_name,
            libc_version=libc_version,
        ),
    }


def _runner_files_root() -> str:
    try:
        return execution_env.runner_files_root(RUNNER_FILES)
    except execution_env.ExecutionEnvironmentError as exc:
        raise ReplayExecutionError(f"cannot verify replay-runner files: {exc}") from exc


def _runtime_facts(
    profile: str,
    trusted_runtime_evidence: Mapping[str, Any] | None,
) -> dict[str, Any]:
    if profile == execution_env.DEVELOPMENT_HOST_PROFILE:
        if trusted_runtime_evidence is not None:
            raise ReplayExecutionError(
                "trusted runtime evidence is valid only for authoritative-oci replay"
            )
        return _development_runtime_facts()
    if trusted_runtime_evidence is None:
        raise ReplayExecutionError(
            "authoritative-oci replay requires explicit trusted runtime evidence"
        )
    try:
        evidence = execution_env.validate_trusted_runtime_evidence(
            dict(trusted_runtime_evidence)
        )
    except execution_env.ExecutionEnvironmentError as exc:
        raise ReplayExecutionError(f"trusted runtime evidence is invalid: {exc}") from exc
    runtime = dict(evidence["runtime"])
    if _host_operating_system() != runtime["os"]:
        raise ReplayExecutionError(
            "trusted OCI runtime operating system disagrees with the live host"
        )
    if platform.machine().lower() != runtime["architecture"]:
        raise ReplayExecutionError(
            "trusted OCI runtime architecture disagrees with the live host"
        )
    return runtime


def _python_command(interpreter: Path, program: Path, *arguments: str) -> tuple[str, ...]:
    return (
        str(interpreter),
        "-P",
        "-S",
        "-s",
        "-B",
        "-c",
        _RUN_STAGE,
        str(program),
        *arguments,
    )


def require_isolated_startup() -> None:
    """Reject a runner process whose Python startup consulted caller paths/env."""
    if not sys.flags.ignore_environment or not sys.flags.safe_path:
        raise ReplayExecutionError(
            "v2 replay requires an isolated launcher: invoke Python with -I"
        )


def _real_directory(path: Path, label: str) -> os.stat_result:
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise ReplayExecutionError(f"{label} is unavailable: {path}: {exc}") from exc
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
        raise ReplayExecutionError(f"{label} is not a real directory: {path}")
    return metadata


def _real_file(path: Path, label: str) -> os.stat_result:
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise ReplayExecutionError(f"{label} is unavailable: {path}: {exc}") from exc
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
        raise ReplayExecutionError(f"{label} is not a regular file: {path}")
    return metadata


def _require_empty_directory(path: Path, label: str) -> None:
    metadata = _real_directory(path, label)
    if stat.S_IMODE(metadata.st_mode) != 0o700:
        raise ReplayExecutionError(f"{label} must have mode 0o700: {path}")
    try:
        first = next(path.iterdir(), None)
    except OSError as exc:
        raise ReplayExecutionError(f"cannot inspect {label}: {path}: {exc}") from exc
    if first is not None:
        raise ReplayExecutionError(f"{label} must be empty before replay: {first}")


def _hash_file(path: Path) -> tuple[int, str]:
    digest = hashlib.sha256()
    size = 0
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    try:
        metadata = os.fstat(descriptor)
        current = path.lstat()
        if (
            not stat.S_ISREG(metadata.st_mode)
            or stat.S_ISLNK(current.st_mode)
            or not os.path.samestat(metadata, current)
        ):
            raise ReplayExecutionError(f"path is not a stable regular file: {path}")
        with os.fdopen(descriptor, "rb") as handle:
            descriptor = -1
            while chunk := handle.read(1024 * 1024):
                size += len(chunk)
                digest.update(chunk)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    return size, digest.hexdigest()


def _verify_read_only_tree(root: Path, label: str) -> set[Path]:
    """Validate a symlink-free read-only tree and return its regular files."""
    root_metadata = _real_directory(root, label)
    if stat.S_IMODE(root_metadata.st_mode) != 0o555:
        raise ReplayExecutionError(f"{label} root must have mode 0o555: {root}")
    files: set[Path] = set()
    for directory, names, filenames in os.walk(root, followlinks=False):
        directory_path = Path(directory)
        metadata = _real_directory(directory_path, label)
        if stat.S_IMODE(metadata.st_mode) != 0o555:
            raise ReplayExecutionError(
                f"{label} directory must have mode 0o555: {directory_path}"
            )
        for name in names:
            child = directory_path / name
            child_metadata = child.lstat()
            if stat.S_ISLNK(child_metadata.st_mode) or not stat.S_ISDIR(
                child_metadata.st_mode
            ):
                raise ReplayExecutionError(
                    f"{label} contains a non-directory entry: {child}"
                )
        for name in filenames:
            child = directory_path / name
            child_metadata = _real_file(child, label)
            if stat.S_IMODE(child_metadata.st_mode) != 0o444:
                raise ReplayExecutionError(
                    f"{label} file must have mode 0o444: {child}"
                )
            files.add(child)
    return files


def _verify_input_closure(context: build_context.BuildContext) -> None:
    actual = _verify_read_only_tree(context.roots.input, "sealed input tree")
    expected: set[Path] = set()
    for binding in context.bindings:
        for member, path in zip(binding.members, context.members(binding.input_id)):
            expected.add(path)
            metadata = _real_file(path, f"input {binding.input_id!r}")
            if metadata.st_nlink != 1:
                raise ReplayExecutionError(f"sealed input must not be hard-linked: {path}")
            if stat.S_IMODE(metadata.st_mode) != 0o444:
                raise ReplayExecutionError(
                    f"sealed input has mode {stat.S_IMODE(metadata.st_mode):#o}, "
                    f"expected 0o444: {path}"
                )
            size, digest = _hash_file(path)
            if size != member.byte_length or digest != member.sha256:
                raise ReplayExecutionError(
                    f"sealed input bytes disagree with the build context: {path}"
                )
    if actual != expected:
        extras = sorted(str(path) for path in actual - expected)
        missing = sorted(str(path) for path in expected - actual)
        raise ReplayExecutionError(
            "sealed input tree does not equal the declared member closure: "
            f"extra={extras[:5]!r}, missing={missing[:5]!r}"
        )


def _verify_code_closure(
    context: build_context.BuildContext,
    reducer_files: tuple[ReducerFileSpec, ...],
) -> tuple[Path, ...]:
    actual = _verify_read_only_tree(context.roots.code, "sealed reducer tree")
    expected: set[Path] = set()
    previous = ""
    for logical_path, byte_length, digest in reducer_files:
        if logical_path <= previous:
            raise ReplayExecutionError(
                "reducer file specifications must be unique and sorted"
            )
        previous = logical_path
        path = context.code(logical_path)
        expected.add(path)
        metadata = _real_file(path, f"reducer file {logical_path!r}")
        if metadata.st_nlink != 1:
            raise ReplayExecutionError(f"sealed reducer file is hard-linked: {path}")
        size, actual_digest = _hash_file(path)
        if size != byte_length or actual_digest != digest:
            raise ReplayExecutionError(
                f"sealed reducer bytes disagree with the verified pack: {logical_path}"
            )
    if actual != expected:
        extras = sorted(str(path) for path in actual - expected)
        missing = sorted(str(path) for path in expected - actual)
        raise ReplayExecutionError(
            "sealed reducer tree does not equal the verified pack closure: "
            f"extra={extras[:5]!r}, missing={missing[:5]!r}"
        )
    return tuple(sorted(expected))


def _verify_execution_environment(
    workspace: Path,
    context: build_context.BuildContext,
) -> dict[str, Any]:
    """Read and validate the exact environment descriptor sealed at workspace root."""
    path = workspace / EXECUTION_ENVIRONMENT_NAME
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise ReplayExecutionError(
            f"sealed execution environment is unavailable: {path}: {exc}"
        ) from exc
    try:
        opened = os.fstat(descriptor)
        current = path.lstat()
        if (
            not stat.S_ISREG(opened.st_mode)
            or stat.S_ISLNK(current.st_mode)
            or not os.path.samestat(opened, current)
        ):
            raise ReplayExecutionError(
                f"sealed execution environment is not a stable regular file: {path}"
            )
        if opened.st_nlink != 1:
            raise ReplayExecutionError(
                f"sealed execution environment must not be hard-linked: {path}"
            )
        if stat.S_IMODE(opened.st_mode) != 0o444:
            raise ReplayExecutionError(
                f"sealed execution environment must have mode 0o444: {path}"
            )
        digest = hashlib.sha256()
        chunks: list[bytes] = []
        while chunk := os.read(descriptor, 1024 * 1024):
            digest.update(chunk)
            chunks.append(chunk)
        finished = os.fstat(descriptor)
        latest = path.lstat()
        opened_state = (
            opened.st_mode,
            opened.st_nlink,
            opened.st_size,
            opened.st_mtime_ns,
            opened.st_ctime_ns,
        )
        finished_state = (
            finished.st_mode,
            finished.st_nlink,
            finished.st_size,
            finished.st_mtime_ns,
            finished.st_ctime_ns,
        )
        latest_state = (
            latest.st_mode,
            latest.st_nlink,
            latest.st_size,
            latest.st_mtime_ns,
            latest.st_ctime_ns,
        )
        if (
            not os.path.samestat(opened, finished)
            or not os.path.samestat(finished, latest)
            or opened_state != finished_state
            or finished_state != latest_state
        ):
            raise ReplayExecutionError(
                f"sealed execution environment changed while being read: {path}"
            )
    finally:
        os.close(descriptor)

    data = b"".join(chunks)
    expected_digest = context.replay.environment_sha256
    if digest.hexdigest() != expected_digest:
        raise ReplayExecutionError(
            "sealed execution environment does not match "
            "context reducer.environment_sha256"
        )
    try:
        document = contracts.parse_json_bytes(data, location=str(path))
        if data != contracts.canonical_json_bytes(document):
            raise ReplayExecutionError(
                "sealed execution environment is not canonical-json-v1 bytes"
            )
        environment = contracts.validate_execution_environment(
            document, location="$.execution_environment"
        )
    except contracts.VerificationError as exc:
        raise ReplayExecutionError(
            f"sealed execution environment is invalid: {exc}"
        ) from exc
    if environment["runner"]["git_commit"] != context.replay.reducer_git_commit:
        raise ReplayExecutionError(
            "sealed execution environment runner Git commit does not match "
            "context reducer Git commit"
        )
    return environment


def _validate_workspace(
    context_path: Path,
    context: build_context.BuildContext,
    reducer_files: tuple[ReducerFileSpec, ...],
) -> tuple[Path, tuple[Path, ...], dict[str, Any]]:
    context_path = context_path.resolve(strict=True)
    workspace = context_path.parent
    if context_path != workspace / "build-context.json":
        raise ReplayExecutionError(
            "the context must be the prepared workspace build-context.json"
        )
    workspace_metadata = _real_directory(workspace, "replay workspace")
    if stat.S_IMODE(workspace_metadata.st_mode) != 0o700:
        raise ReplayExecutionError("replay workspace must have mode 0o700")
    expected_entries = {
        "build-context.json",
        "code",
        EXECUTION_ENVIRONMENT_NAME,
        "input",
        "output",
        "scratch",
    }
    try:
        actual_entries = {entry.name for entry in workspace.iterdir()}
    except OSError as exc:
        raise ReplayExecutionError(
            f"cannot inspect replay workspace: {workspace}: {exc}"
        ) from exc
    if actual_entries != expected_entries:
        raise ReplayExecutionError(
            "replay workspace does not equal the prepared closure: "
            f"extra={sorted(actual_entries - expected_entries)!r}, "
            f"missing={sorted(expected_entries - actual_entries)!r}"
        )
    expected_roots = {
        name: workspace / name for name in ("code", "input", "output", "scratch")
    }
    for name, expected in expected_roots.items():
        actual = getattr(context.roots, name)
        if actual != expected:
            raise ReplayExecutionError(
                f"context root {name!r} is {actual}, expected {expected}"
            )
    context_metadata = _real_file(context_path, "build context")
    if context_metadata.st_nlink != 1:
        raise ReplayExecutionError("build-context.json must not be hard-linked")
    if stat.S_IMODE(context_metadata.st_mode) != 0o444:
        raise ReplayExecutionError("build-context.json must have mode 0o444")
    execution_environment = _verify_execution_environment(workspace, context)
    _verify_input_closure(context)
    code_files = set(_verify_code_closure(context, reducer_files))
    programs: list[Path] = []
    for stage in context.stages:
        program = context.code(stage.program)
        if program not in code_files:
            raise ReplayExecutionError(
                f"stage {stage.id!r} program is absent from the reducer tree: {program}"
            )
        programs.append(program)
    _require_empty_directory(context.roots.output, "replay output root")
    _require_empty_directory(context.roots.scratch, "replay scratch root")
    return workspace, tuple(programs), execution_environment


def _scheme_string(value: Path) -> str:
    return json.dumps(str(value), ensure_ascii=True)


def _runtime_layout(
    interpreter: Path,
) -> tuple[tuple[Path, ...], Mapping[str, Path]]:
    """Return verified runtime mounts and the parent's package-scheme anchors."""
    current = Path(sys.executable).resolve(strict=True)
    if interpreter != current:
        raise ReplayExecutionError(
            "the replay interpreter must resolve to the interpreter running the runner"
        )
    base_prefixes: list[Path] = []
    for value in (sys.base_prefix, sys.base_exec_prefix):
        path = Path(value).resolve(strict=True)
        if path not in base_prefixes:
            base_prefixes.append(path)
    if not any(
        interpreter == prefix or prefix in interpreter.parents
        for prefix in base_prefixes
    ):
        raise ReplayExecutionError(
            "the replay interpreter is outside the running Python base prefix"
        )
    runtime_prefixes: list[Path] = []
    for value in (sys.prefix, sys.exec_prefix):
        path = Path(value).resolve(strict=True)
        if path not in runtime_prefixes:
            runtime_prefixes.append(path)
    roots = list(base_prefixes)
    scheme_paths: dict[str, Path] = {}
    configured = sysconfig.get_paths()
    for key in ("purelib", "platlib"):
        item = configured.get(key)
        if not item:
            raise ReplayExecutionError(
                f"Python package scheme does not define {key!r}"
            )
        try:
            path = Path(item).resolve(strict=True)
        except OSError as exc:
            raise ReplayExecutionError(
                f"Python package root is unavailable: {item}: {exc}"
            ) from exc
        if not any(
            path == prefix or prefix in path.parents for prefix in runtime_prefixes
        ):
            raise ReplayExecutionError(
                f"Python package root escapes the runtime prefixes: {path}"
            )
        if path not in roots:
            roots.append(path)
        scheme_paths[key] = path
    for root in roots:
        _real_directory(root, "Python runtime root")
    return tuple(roots), MappingProxyType(scheme_paths)


def _runtime_roots(interpreter: Path) -> tuple[Path, ...]:
    """Return the host runtime trees required by this runner's interpreter."""
    return _runtime_layout(interpreter)[0]


def _runtime_scheme_paths(interpreter: Path) -> Mapping[str, Path]:
    """Return exact parent purelib/platlib paths for the isolated child probe."""
    return _runtime_layout(interpreter)[1]


def _runtime_pythonpath(interpreter: Path) -> str:
    scheme_paths = _runtime_scheme_paths(interpreter)
    roots: list[Path] = []
    for key in ("purelib", "platlib"):
        root = scheme_paths[key]
        if root not in roots:
            roots.append(root)
    return os.pathsep.join(str(root) for root in roots)


def _darwin_boundary(
    context: build_context.BuildContext, interpreter: Path
) -> IsolationBoundary:
    executable = Path("/usr/bin/sandbox-exec")
    _real_file(executable, "Darwin sandbox executable")
    workspace = _scheme_string(context.roots.output.parent)
    runtime_rules = " ".join(
        f"(subpath {_scheme_string(path)})" for path in _runtime_roots(interpreter)
    )
    python = _scheme_string(interpreter)
    output = _scheme_string(context.roots.output)
    scratch = _scheme_string(context.roots.scratch)
    profile = " ".join(
        (
            "(version 1)",
            "(deny default)",
            '(import "system.sb")',
            f"(allow process-exec (literal {python}))",
            "(deny process-fork)",
            f"(allow file-read* (subpath {workspace}) {runtime_rules})",
            f"(allow file-map-executable {runtime_rules})",
            f"(allow file-write* (subpath {output}) (subpath {scratch}))",
            "(deny network*)",
        )
    )
    return IsolationBoundary(
        "darwin-sandbox-exec",
        (str(executable), "-p", profile),
        executable,
        _sandbox_policy_document("darwin-sandbox-exec"),
    )


def _linux_runtime_mounts(
    runtime_roots: tuple[Path, ...],
) -> tuple[tuple[Path, Path], ...]:
    """Map runtime bytes while preserving merged-/usr loader aliases."""
    mounts: list[tuple[Path, Path]] = [(root, root) for root in runtime_roots]
    for destination in (
        Path("/lib"),
        Path("/lib64"),
        Path("/usr/lib"),
        Path("/usr/lib64"),
        Path("/etc/ld.so.cache"),
    ):
        if not destination.exists():
            continue
        source = destination.resolve(strict=True)
        if any(
            destination == mounted_destination
            or (
                mounted_source == mounted_destination
                and mounted_destination in destination.parents
            )
            for mounted_source, mounted_destination in mounts
        ):
            continue
        mounts.append((source, destination))
    return tuple(mounts)


def _linux_boundary(
    context: build_context.BuildContext, interpreter: Path
) -> IsolationBoundary:
    executable = Path("/usr/bin/bwrap")
    _real_file(executable, "Linux bubblewrap executable")
    runtime_roots = _runtime_roots(interpreter)
    workspace = str(context.roots.output.parent)
    output = str(context.roots.output)
    scratch = str(context.roots.scratch)
    runtime_mounts = tuple(
        argument
        for source, destination in _linux_runtime_mounts(runtime_roots)
        for argument in ("--ro-bind", str(source), str(destination))
    )
    return IsolationBoundary(
        "linux-bubblewrap",
        (
            str(executable),
            "--die-with-parent",
            "--new-session",
            "--unshare-all",
            "--cap-drop",
            "ALL",
            "--dev",
            "/dev",
            "--proc",
            "/proc",
            "--tmpfs",
            "/tmp",
            *runtime_mounts,
            "--ro-bind",
            workspace,
            workspace,
            "--bind",
            output,
            output,
            "--bind",
            scratch,
            scratch,
            "--chdir",
            str(context.roots.code),
            "--",
        ),
        executable,
        _sandbox_policy_document("linux-bubblewrap"),
    )


def _select_isolation(
    context: build_context.BuildContext, interpreter: Path
) -> IsolationBoundary:
    if sys.platform == "darwin":
        return _darwin_boundary(context, interpreter)
    if sys.platform.startswith("linux"):
        return _linux_boundary(context, interpreter)
    raise ReplayExecutionError(
        f"no supported kernel isolation boundary for platform {sys.platform!r}"
    )


def _environment(interpreter: Path) -> dict[str, str]:
    """Return the complete reducer environment; nothing is inherited."""
    return {
        "BLIS_NUM_THREADS": "1",
        "HOME": "/nonexistent",
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "MKL_NUM_THREADS": "1",
        "NUMEXPR_NUM_THREADS": "1",
        "OMP_NUM_THREADS": "1",
        "OMP_THREAD_LIMIT": "1",
        "OPENBLAS_NUM_THREADS": "1",
        "PATH": "/nonexistent",
        "PYTHONPATH": _runtime_pythonpath(interpreter),
        "PYTHONHASHSEED": "0",
        "PYTHONIOENCODING": "utf-8",
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONNOUSERSITE": "1",
        "PYTHONUTF8": "1",
        "TZ": "UTC",
        "VECLIB_MAXIMUM_THREADS": "1",
        "WIKILEAN_OFFLINE": "1",
    }


def _execute(
    command: tuple[str, ...],
    cwd: Path,
    environment: Mapping[str, str],
    isolation: IsolationBoundary,
    *,
    timeout_seconds: float = DEFAULT_STAGE_TIMEOUT_SECONDS,
) -> int:
    timeout = _validated_stage_timeout_seconds(timeout_seconds)
    try:
        process = subprocess.Popen(
            [*isolation.prefix, *command],
            cwd=cwd,
            env=dict(environment),
            start_new_session=True,
        )
    except OSError as exc:
        raise ReplayExecutionError(f"cannot start reducer stage subprocess: {exc}") from exc
    try:
        return process.wait(timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        _terminate_process(process)
        raise ReplayExecutionError(
            "reducer stage subprocess timed out after "
            f"{timeout:g} seconds"
        ) from exc


def _terminate_process(process: subprocess.Popen[bytes]) -> None:
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    except OSError:
        try:
            process.kill()
        except ProcessLookupError:
            pass
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        try:
            process.kill()
        except ProcessLookupError:
            pass
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            # SIGKILL has already been requested. Never turn timeout cleanup into
            # an unbounded wait on a process stuck in uninterruptible kernel I/O.
            pass


def _validated_stage_timeout_seconds(value: Any) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ReplayExecutionError("stage timeout must be a finite positive number")
    timeout = float(value)
    if not math.isfinite(timeout) or timeout <= 0:
        raise ReplayExecutionError("stage timeout must be a finite positive number")
    return timeout


def _capture_process(
    command: tuple[str, ...],
    cwd: Path,
    environment: Mapping[str, str],
    *,
    timeout_seconds: float,
    stdout_limit: int,
    stderr_limit: int,
) -> tuple[int, bytes, bytes]:
    """Run a command with a hard deadline and bounded stdout/stderr buffers."""
    if timeout_seconds <= 0 or stdout_limit < 0 or stderr_limit < 0:
        raise ReplayExecutionError("invalid bounded-process limits")
    try:
        process = subprocess.Popen(
            command,
            cwd=cwd,
            env=dict(environment),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )
    except OSError as exc:
        raise ReplayExecutionError(f"cannot start execution-environment probe: {exc}") from exc
    assert process.stdout is not None
    assert process.stderr is not None
    selector = selectors.DefaultSelector()
    buffers = {"stdout": bytearray(), "stderr": bytearray()}
    limits = {"stdout": stdout_limit, "stderr": stderr_limit}
    streams = {"stdout": process.stdout, "stderr": process.stderr}
    deadline = time.monotonic() + timeout_seconds
    try:
        for name, stream in streams.items():
            os.set_blocking(stream.fileno(), False)
            selector.register(stream, selectors.EVENT_READ, name)
        while selector.get_map():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise ReplayExecutionError("execution-environment probe timed out")
            events = selector.select(remaining)
            if not events:
                raise ReplayExecutionError("execution-environment probe timed out")
            for key, _mask in events:
                name = key.data
                stream = key.fileobj
                allowance = limits[name] - len(buffers[name]) + 1
                try:
                    chunk = os.read(stream.fileno(), min(64 * 1024, max(1, allowance)))
                except BlockingIOError:
                    continue
                if not chunk:
                    selector.unregister(stream)
                    continue
                buffers[name].extend(chunk)
                if len(buffers[name]) > limits[name]:
                    raise ReplayExecutionError(
                        f"execution-environment probe {name} exceeded its byte limit"
                    )
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise ReplayExecutionError("execution-environment probe timed out")
        try:
            returncode = process.wait(timeout=remaining)
        except subprocess.TimeoutExpired as exc:
            raise ReplayExecutionError(
                "execution-environment probe timed out"
            ) from exc
    except BaseException:
        _terminate_process(process)
        raise
    finally:
        selector.close()
        process.stdout.close()
        process.stderr.close()
    return returncode, bytes(buffers["stdout"]), bytes(buffers["stderr"])


def _execute_probe(
    command: tuple[str, ...],
    cwd: Path,
    environment: Mapping[str, str],
    isolation: IsolationBoundary,
) -> tuple[int, bytes, bytes]:
    return _capture_process(
        (*isolation.prefix, *command),
        cwd,
        environment,
        timeout_seconds=PROBE_TIMEOUT_SECONDS,
        stdout_limit=PROBE_STDOUT_LIMIT,
        stderr_limit=PROBE_STDERR_LIMIT,
    )


def _sandbox_reported_version(executable: Path, backend: str) -> str | None:
    if backend == "darwin-sandbox-exec":
        return None
    if backend != "linux-bubblewrap":
        raise ReplayExecutionError(f"unknown replay sandbox backend {backend!r}")
    returncode, stdout, stderr = _capture_process(
        (str(executable), "--version"),
        HERE,
        {
            "HOME": "/nonexistent",
            "LANG": "C.UTF-8",
            "LC_ALL": "C.UTF-8",
            "PATH": "/nonexistent",
            "TZ": "UTC",
        },
        timeout_seconds=SANDBOX_VERSION_TIMEOUT_SECONDS,
        stdout_limit=4096,
        stderr_limit=4096,
    )
    if returncode != 0 or stderr:
        raise ReplayExecutionError(
            "cannot obtain an exact bubblewrap version from the sandbox executable"
        )
    try:
        output = stdout.decode("ascii")
    except UnicodeDecodeError as exc:
        raise ReplayExecutionError("bubblewrap version output is not ASCII") from exc
    if output.endswith("\n"):
        output = output[:-1]
    match = _BUBBLEWRAP_VERSION_RE.fullmatch(output)
    if match is None:
        raise ReplayExecutionError("bubblewrap version output is malformed")
    return match.group(1)


def _sandbox_facts(isolation: IsolationBoundary) -> dict[str, Any]:
    if isolation.executable is None or isolation.policy is None:
        raise ReplayExecutionError("selected sandbox lacks identity metadata")
    if not isolation.prefix or isolation.prefix[0] != str(isolation.executable):
        raise ReplayExecutionError(
            "selected sandbox command disagrees with its executable identity"
        )
    expected_policy = _sandbox_policy_document(isolation.name)
    if dict(isolation.policy) != expected_policy:
        raise ReplayExecutionError(
            "selected sandbox command disagrees with its structural policy"
        )
    _real_file(isolation.executable, "sandbox executable")
    try:
        executable_digest, _byte_length = execution_env.secure_file_digest(
            isolation.executable
        )
        policy_root = execution_env.sandbox_policy_root(expected_policy)
    except execution_env.ExecutionEnvironmentError as exc:
        raise ReplayExecutionError(f"cannot verify sandbox identity: {exc}") from exc
    return {
        "backend": isolation.name,
        "reported_version": _sandbox_reported_version(
            isolation.executable, isolation.name
        ),
        "executable_sha256": executable_digest,
        "policy_id": SANDBOX_POLICY_ID,
        "policy_root": policy_root,
        "network": "disabled",
    }


@contextmanager
def _materialized_probe_program(
    context: build_context.BuildContext,
) -> Iterator[Path]:
    """Stage exact probe support bytes under scratch, then remove them."""
    support_root = context.roots.scratch / ".execution-environment-probe"
    try:
        support_root.mkdir(mode=0o700)
    except OSError as exc:
        raise ReplayExecutionError(
            f"cannot create execution-environment probe support: {exc}"
        ) from exc
    source_files = {
        "execution_environment.py": HERE / "execution_environment.py",
        "probe_execution_environment.py": PROBE_PROGRAM,
    }
    try:
        for name, source in source_files.items():
            try:
                source_digest, source_size = execution_env.secure_file_digest(source)
                data = source.read_bytes()
                if (
                    len(data) != source_size
                    or hashlib.sha256(data).hexdigest() != source_digest
                    or execution_env.secure_file_digest(source)
                    != (source_digest, source_size)
                ):
                    raise ReplayExecutionError(
                        f"probe support source changed while being copied: {source}"
                    )
            except (OSError, execution_env.ExecutionEnvironmentError) as exc:
                raise ReplayExecutionError(
                    f"cannot read execution-environment probe support: {exc}"
                ) from exc
            destination = support_root / name
            descriptor = os.open(
                destination,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0),
                0o600,
            )
            try:
                view = memoryview(data)
                while view:
                    written = os.write(descriptor, view)
                    if written <= 0:
                        raise ReplayExecutionError(
                            "short write while staging execution-environment probe"
                        )
                    view = view[written:]
                os.fchmod(descriptor, 0o444)
            finally:
                os.close(descriptor)
            try:
                copied_digest, copied_size = execution_env.secure_file_digest(
                    destination
                )
            except execution_env.ExecutionEnvironmentError as exc:
                raise ReplayExecutionError(
                    f"cannot verify staged execution-environment probe: {exc}"
                ) from exc
            if (copied_digest, copied_size) != (source_digest, source_size):
                raise ReplayExecutionError(
                    "staged execution-environment probe bytes are not exact"
                )
        support_root.chmod(0o555)
        yield support_root / "probe_execution_environment.py"
    finally:
        try:
            metadata = support_root.lstat()
            if stat.S_ISLNK(metadata.st_mode):
                support_root.unlink()
            elif stat.S_ISDIR(metadata.st_mode):
                support_root.chmod(0o700)
                shutil.rmtree(support_root)
            else:
                support_root.unlink()
        except FileNotFoundError:
            pass
        except OSError as exc:
            raise ReplayExecutionError(
                f"cannot remove execution-environment probe support: {exc}"
            ) from exc


def _read_live_probe(
    context: build_context.BuildContext,
    interpreter: Path,
    environment: Mapping[str, str],
    isolation: IsolationBoundary,
    probe_executor: ProbeExecutor,
) -> dict[str, Any]:
    try:
        with _materialized_probe_program(context) as probe_program:
            scheme_paths = _runtime_scheme_paths(interpreter)
            command = _python_command(
                interpreter,
                probe_program,
                "--purelib",
                str(scheme_paths["purelib"]),
                "--platlib",
                str(scheme_paths["platlib"]),
            )
            result = probe_executor(
                command, context.roots.code, environment, isolation
            )
    except ReplayExecutionError:
        raise
    except subprocess.TimeoutExpired as exc:
        raise ReplayExecutionError("execution-environment probe timed out") from exc
    except Exception as exc:
        raise ReplayExecutionError(
            f"execution-environment probe could not run: {exc}"
        ) from exc
    if (
        not isinstance(result, tuple)
        or len(result) != 3
        or type(result[0]) is not int
        or not isinstance(result[1], bytes)
        or not isinstance(result[2], bytes)
    ):
        raise ReplayExecutionError("execution-environment probe returned an invalid result")
    returncode, stdout, stderr = result
    if len(stdout) > PROBE_STDOUT_LIMIT or len(stderr) > PROBE_STDERR_LIMIT:
        raise ReplayExecutionError("execution-environment probe output exceeded its byte limit")
    if returncode != 0:
        detail = stderr.decode("utf-8", errors="replace")[:500]
        raise ReplayExecutionError(
            f"execution-environment probe failed with status {returncode}"
            + (f": {detail}" if detail else "")
        )
    if stderr:
        raise ReplayExecutionError("execution-environment probe produced unexpected stderr")
    try:
        document = contracts.parse_json_bytes(
            stdout, location="execution-environment probe stdout"
        )
        if stdout != contracts.canonical_json_bytes(document):
            raise ReplayExecutionError(
                "execution-environment probe output is not canonical-json-v1"
            )
        return execution_env.validate_live_probe_document(document)
    except (contracts.VerificationError, execution_env.ExecutionEnvironmentError) as exc:
        raise ReplayExecutionError(
            f"execution-environment probe output is invalid: {exc}"
        ) from exc


def _probe_execution_environment(
    *,
    profile: str,
    runtime: dict[str, Any],
    runner_root: str,
    parent_python: dict[str, Any],
    context: build_context.BuildContext,
    interpreter: Path,
    environment: Mapping[str, str],
    isolation: IsolationBoundary,
    sandbox: dict[str, Any],
    probe_executor: ProbeExecutor = _execute_probe,
) -> dict[str, Any]:
    """Measure parent and child runtime facts without echoing descriptor values."""
    try:
        child = _read_live_probe(
            context, interpreter, environment, isolation, probe_executor
        )
        if child["python"] != parent_python:
            raise ReplayExecutionError(
                "sandboxed Python facts disagree with the parent interpreter"
            )
        return execution_env.probe_live_environment_projection(
            profile=profile,
            runtime_probe=lambda: runtime,
            runner_files_probe=lambda: runner_root,
            python_probe=lambda: child["python"],
            numpy_probe=lambda: child["numpy"],
            sqlite_probe=lambda: child["sqlite"],
            locale_probe=lambda: child["locale"],
            sandbox_probe=lambda: sandbox,
        )
    except execution_env.ExecutionEnvironmentError as exc:
        raise ReplayExecutionError(
            f"cannot validate live execution environment: {exc}"
        ) from exc


def _first_projection_difference(expected: Any, actual: Any, location: str = "$") -> str:
    if type(expected) is not type(actual):
        return location
    if isinstance(expected, dict):
        if expected.keys() != actual.keys():
            return location
        for key in expected:
            difference = _first_projection_difference(
                expected[key], actual[key], f"{location}.{key}"
            )
            if difference:
                return difference
        return ""
    if isinstance(expected, list):
        if len(expected) != len(actual):
            return location
        for index, (left, right) in enumerate(zip(expected, actual, strict=True)):
            difference = _first_projection_difference(
                left, right, f"{location}[{index}]"
            )
            if difference:
                return difference
        return ""
    return "" if expected == actual else location


def _is_within(path: PurePosixPath, root: PurePosixPath) -> bool:
    return path == root or root in path.parents


def _verify_outputs(
    context: build_context.BuildContext,
    completed_stage_ids: tuple[str, ...],
) -> None:
    output_root_metadata = _real_directory(
        context.roots.output, "replay output root"
    )
    if stat.S_IMODE(output_root_metadata.st_mode) != 0o700:
        raise ReplayExecutionError("replay output root must retain mode 0o700")
    completed = [context.stage(stage_id) for stage_id in completed_stage_ids]
    file_outputs = {
        PurePosixPath(output.path)
        for stage in completed
        for output in stage.outputs
        if output.kind == "file"
    }
    tree_outputs = {
        PurePosixPath(output.path)
        for stage in completed
        for output in stage.outputs
        if output.kind == "tree"
    }
    all_outputs = file_outputs | tree_outputs
    for stage in completed:
        for output in stage.outputs:
            path = context.output_for(stage.id, output.path)
            metadata = path.lstat() if path.exists() or path.is_symlink() else None
            if metadata is None:
                raise ReplayExecutionError(
                    f"stage {stage.id!r} did not create {output.path!r}"
                )
            if stat.S_ISLNK(metadata.st_mode):
                raise ReplayExecutionError(
                    f"stage {stage.id!r} created a symlink output: {path}"
                )
            expected_type = stat.S_ISREG if output.kind == "file" else stat.S_ISDIR
            if not expected_type(metadata.st_mode):
                raise ReplayExecutionError(
                    f"stage {stage.id!r} created the wrong output type: {path}"
                )
            expected_mode = (
                0o600
                if stage.id == "sqlite-with-cells"
                and output.path == "brain/data/brain.sqlite3"
                else 0o644
                if output.kind == "file"
                else 0o700
            )
            if stat.S_IMODE(metadata.st_mode) != expected_mode:
                raise ReplayExecutionError(
                    f"stage {stage.id!r} output mode is not {expected_mode:#o}: {path}"
                )

    for directory, names, filenames in os.walk(
        context.roots.output, followlinks=False
    ):
        directory_path = Path(directory)
        if directory_path != context.roots.output:
            relative = PurePosixPath(
                directory_path.relative_to(context.roots.output).as_posix()
            )
            allowed = any(_is_within(relative, tree) for tree in tree_outputs) or any(
                _is_within(output, relative) for output in all_outputs
            )
            metadata = directory_path.lstat()
            if (
                not allowed
                or stat.S_ISLNK(metadata.st_mode)
                or not stat.S_ISDIR(metadata.st_mode)
            ):
                raise ReplayExecutionError(
                    f"undeclared output directory created: {directory_path}"
                )
            if stat.S_IMODE(metadata.st_mode) != 0o700:
                raise ReplayExecutionError(
                    f"output directory mode is not 0o700: {directory_path}"
                )
        for name in names:
            child = directory_path / name
            if child.is_symlink():
                raise ReplayExecutionError(f"output symlink is forbidden: {child}")
        for name in filenames:
            child = directory_path / name
            relative = PurePosixPath(child.relative_to(context.roots.output).as_posix())
            allowed = relative in file_outputs or any(
                _is_within(relative, tree) for tree in tree_outputs
            )
            metadata = child.lstat()
            if (
                not allowed
                or stat.S_ISLNK(metadata.st_mode)
                or not stat.S_ISREG(metadata.st_mode)
            ):
                raise ReplayExecutionError(f"undeclared output file created: {child}")
            expected_mode = (
                0o600
                if relative == PurePosixPath("brain/data/brain.sqlite3")
                else 0o644
            )
            if stat.S_IMODE(metadata.st_mode) != expected_mode:
                raise ReplayExecutionError(
                    f"output file mode is not {expected_mode:#o}: {child}"
                )


def _verify_scratch(
    context: build_context.BuildContext,
    completed_stage_ids: tuple[str, ...],
) -> None:
    scratch_root_metadata = _real_directory(
        context.roots.scratch, "replay scratch root"
    )
    if stat.S_IMODE(scratch_root_metadata.st_mode) != 0o700:
        raise ReplayExecutionError("replay scratch root must retain mode 0o700")
    completed = set(completed_stage_ids)
    for directory, names, filenames in os.walk(
        context.roots.scratch, followlinks=False
    ):
        directory_path = Path(directory)
        if filenames:
            raise ReplayExecutionError(
                f"stage left files in replay scratch: {directory_path / filenames[0]}"
            )
        if directory_path != context.roots.scratch:
            relative = directory_path.relative_to(context.roots.scratch)
            metadata = directory_path.lstat()
            if (
                relative.parts[0] not in completed
                or stat.S_ISLNK(metadata.st_mode)
                or not stat.S_ISDIR(metadata.st_mode)
            ):
                raise ReplayExecutionError(
                    f"stage left undeclared scratch state: {directory_path}"
                )
            if stat.S_IMODE(metadata.st_mode) != 0o700:
                raise ReplayExecutionError(
                    f"scratch directory mode is not 0o700: {directory_path}"
                )
        for name in names:
            child = directory_path / name
            if child.is_symlink():
                raise ReplayExecutionError(f"scratch symlink is forbidden: {child}")


def _output_state(
    context: build_context.BuildContext,
    completed_stage_ids: tuple[str, ...],
) -> dict[str, OutputState]:
    """Capture identity metadata and bytes for every completed output entry."""
    state: dict[str, OutputState] = {}

    def record(path: Path) -> None:
        metadata = path.lstat()
        relative = path.relative_to(context.roots.output).as_posix()
        digest: str | None = None
        if stat.S_ISREG(metadata.st_mode):
            size, digest = _hash_file(path)
            if size != metadata.st_size:
                raise ReplayExecutionError(
                    f"output changed while its state was captured: {path}"
                )
        state[relative] = (
            stat.S_IFMT(metadata.st_mode),
            stat.S_IMODE(metadata.st_mode),
            metadata.st_dev,
            metadata.st_ino,
            metadata.st_size,
            metadata.st_ctime_ns,
            digest,
        )

    for stage_id in completed_stage_ids:
        stage = context.stage(stage_id)
        for output in stage.outputs:
            path = context.output_for(stage_id, output.path)
            record(path)
            if output.kind == "tree":
                for directory, names, filenames in os.walk(path, followlinks=False):
                    directory_path = Path(directory)
                    if directory_path != path:
                        record(directory_path)
                    for name in names:
                        child = directory_path / name
                        if child.is_symlink():
                            raise ReplayExecutionError(
                                f"output symlink is forbidden: {child}"
                            )
                    for name in filenames:
                        record(directory_path / name)
    return state


def run_replay_v2(
    context_path: str | os.PathLike[str],
    *,
    reducer_files: tuple[ReducerFileSpec, ...],
    expected_generation_id: str,
    expected_offline_pack_id: str,
    expected_source_set_root: str,
    expected_reducer_inventory_id: str,
    expected_reducer_git_commit: str,
    expected_configuration_sha256: str,
    expected_environment_sha256: str,
    interpreter: str | os.PathLike[str] = sys.executable,
    stage_timeout_seconds: float = DEFAULT_STAGE_TIMEOUT_SECONDS,
    _trusted_runtime_evidence: Mapping[str, Any] | None = None,
    _executor: Executor | None = None,
    _isolation: IsolationBoundary | None = None,
    _probe_executor: ProbeExecutor | None = None,
    _sandbox_probe: SandboxProbe | None = None,
) -> ReplayResult:
    """Run a prepared replay.

    Underscore-prefixed seams are unavailable from the CLI. In particular, no
    production entry point currently supplies trusted OCI launcher evidence,
    so authoritative-oci descriptors fail closed.
    """
    require_isolated_startup()
    stage_timeout = _validated_stage_timeout_seconds(stage_timeout_seconds)
    source = Path(context_path).resolve(strict=True)
    context = build_context.BuildContext.load(source)
    expected_identity = (
        expected_generation_id,
        expected_offline_pack_id,
        expected_source_set_root,
        expected_reducer_inventory_id,
        expected_reducer_git_commit,
        expected_configuration_sha256,
        expected_environment_sha256,
    )
    actual_identity = (
        context.generation_id,
        context.replay.offline_pack_id,
        context.replay.source_set_root,
        context.replay.reducer_inventory_id,
        context.replay.reducer_git_commit,
        context.replay.configuration_sha256,
        context.replay.environment_sha256,
    )
    if actual_identity != expected_identity:
        raise ReplayExecutionError(
            "prepared build context does not match the requested replay identity"
        )
    _workspace, programs, descriptor = _validate_workspace(
        source, context, reducer_files
    )
    requested_python = Path(os.path.abspath(os.fspath(interpreter)))
    resolved_python = requested_python.resolve(strict=True)
    _real_file(resolved_python, "Python interpreter")
    _runtime_roots(resolved_python)
    interpreter_state = _hash_file(resolved_python)
    runtime_facts = _runtime_facts(
        descriptor["profile"], _trusted_runtime_evidence
    )
    if runtime_facts != descriptor["runtime"]:
        raise ReplayExecutionError(
            "live runtime identity disagrees with the sealed execution environment"
        )
    runner_root = _runner_files_root()
    if runner_root != descriptor["runner"]["files_root"]:
        raise ReplayExecutionError(
            "replay-runner file closure disagrees with the sealed execution environment"
        )
    try:
        parent_python = execution_env.probe_python_runtime(
            executable_path=resolved_python
        )
    except execution_env.ExecutionEnvironmentError as exc:
        raise ReplayExecutionError(f"cannot verify Python interpreter: {exc}") from exc
    if parent_python != descriptor["python"]:
        raise ReplayExecutionError(
            "parent Python interpreter disagrees with the sealed execution environment"
        )
    isolation = _isolation or _select_isolation(context, resolved_python)
    if isolation.name != descriptor["sandbox"]["backend"]:
        raise ReplayExecutionError(
            "selected sandbox backend disagrees with the sealed execution environment"
        )
    if _executor is None:

        def executor(
            command: tuple[str, ...],
            cwd: Path,
            child_environment: Mapping[str, str],
            boundary: IsolationBoundary,
        ) -> int:
            return _execute(
                command,
                cwd,
                child_environment,
                boundary,
                timeout_seconds=stage_timeout,
            )
    else:
        executor = _executor
    environment = _environment(resolved_python)
    try:
        sandbox_facts = (_sandbox_probe or _sandbox_facts)(isolation)
    except execution_env.ExecutionEnvironmentError as exc:
        raise ReplayExecutionError(f"cannot verify sandbox identity: {exc}") from exc
    if sandbox_facts != descriptor["sandbox"]:
        difference = _first_projection_difference(
            descriptor["sandbox"], sandbox_facts, "$.sandbox"
        )
        raise ReplayExecutionError(
            "live sandbox identity disagrees with the sealed descriptor"
            + (f" at {difference}" if difference else "")
        )
    expected_projection = execution_env.live_environment_projection(descriptor)
    actual_projection = _probe_execution_environment(
        profile=descriptor["profile"],
        runtime=runtime_facts,
        runner_root=runner_root,
        parent_python=parent_python,
        context=context,
        interpreter=resolved_python,
        environment=environment,
        isolation=isolation,
        sandbox=sandbox_facts,
        probe_executor=_probe_executor or _execute_probe,
    )
    try:
        execution_env.validate_live_environment_projection(actual_projection)
    except execution_env.ExecutionEnvironmentError as exc:
        raise ReplayExecutionError(
            f"live execution-environment projection is invalid: {exc}"
        ) from exc
    if (
        execution_env.canonical_json_bytes(actual_projection)
        != execution_env.canonical_json_bytes(expected_projection)
    ):
        difference = _first_projection_difference(
            expected_projection, actual_projection
        )
        raise ReplayExecutionError(
            "live execution environment disagrees with the sealed descriptor"
            + (f" at {difference}" if difference else "")
        )
    sandbox_executable_state = (
        _hash_file(isolation.executable)
        if isolation.executable is not None
        else None
    )
    completed: list[str] = []
    prior_output_state: dict[str, OutputState] = {}

    for stage, program in zip(context.stages, programs):
        if _hash_file(resolved_python) != interpreter_state:
            raise ReplayExecutionError("Python interpreter changed during replay")
        if _runner_files_root() != runner_root:
            raise ReplayExecutionError("replay-runner files changed during replay")
        if (
            isolation.executable is not None
            and _hash_file(isolation.executable) != sandbox_executable_state
        ):
            raise ReplayExecutionError("sandbox executable changed during replay")
        command = _python_command(
            resolved_python,
            program,
            *stage.argv,
            "--build-context",
            str(source),
            "--stage-id",
            stage.id,
        )
        try:
            returncode = executor(command, context.roots.code, environment, isolation)
        except ReplayExecutionError as exc:
            raise ReplayExecutionError(f"stage {stage.id!r}: {exc}") from exc
        if returncode != 0:
            raise ReplayExecutionError(
                f"stage {stage.id!r} failed with exit status {returncode}"
            )
        completed.append(stage.id)
        completed_ids = tuple(completed)
        if _hash_file(resolved_python) != interpreter_state:
            raise ReplayExecutionError("Python interpreter changed during replay")
        if _runner_files_root() != runner_root:
            raise ReplayExecutionError("replay-runner files changed during replay")
        if (
            isolation.executable is not None
            and _hash_file(isolation.executable) != sandbox_executable_state
        ):
            raise ReplayExecutionError("sandbox executable changed during replay")
        _verify_code_closure(context, reducer_files)
        _verify_outputs(context, completed_ids)
        _verify_scratch(context, completed_ids)
        current_output_state = _output_state(context, completed_ids)
        for relative, previous in prior_output_state.items():
            if current_output_state.get(relative) != previous:
                raise ReplayExecutionError(
                    f"stage {stage.id!r} modified predecessor output {relative!r}"
                )
        prior_output_state = current_output_state

    final_runtime_facts = _runtime_facts(
        descriptor["profile"], _trusted_runtime_evidence
    )
    if final_runtime_facts != descriptor["runtime"]:
        raise ReplayExecutionError("live runtime identity changed during replay")
    final_runner_root = _runner_files_root()
    if final_runner_root != descriptor["runner"]["files_root"]:
        raise ReplayExecutionError("replay-runner file closure changed during replay")
    try:
        final_parent_python = execution_env.probe_python_runtime(
            executable_path=resolved_python
        )
    except execution_env.ExecutionEnvironmentError as exc:
        raise ReplayExecutionError(
            f"cannot reverify Python interpreter after replay: {exc}"
        ) from exc
    if final_parent_python != descriptor["python"]:
        raise ReplayExecutionError("Python interpreter identity changed during replay")
    try:
        final_sandbox_facts = (_sandbox_probe or _sandbox_facts)(isolation)
    except execution_env.ExecutionEnvironmentError as exc:
        raise ReplayExecutionError(
            f"cannot reverify sandbox identity after replay: {exc}"
        ) from exc
    if final_sandbox_facts != descriptor["sandbox"]:
        difference = _first_projection_difference(
            descriptor["sandbox"], final_sandbox_facts, "$.sandbox"
        )
        raise ReplayExecutionError(
            "live sandbox identity changed during replay"
            + (f" at {difference}" if difference else "")
        )
    final_projection = _probe_execution_environment(
        profile=descriptor["profile"],
        runtime=final_runtime_facts,
        runner_root=final_runner_root,
        parent_python=final_parent_python,
        context=context,
        interpreter=resolved_python,
        environment=environment,
        isolation=isolation,
        sandbox=final_sandbox_facts,
        probe_executor=_probe_executor or _execute_probe,
    )
    try:
        execution_env.validate_live_environment_projection(final_projection)
    except execution_env.ExecutionEnvironmentError as exc:
        raise ReplayExecutionError(
            f"final live execution-environment projection is invalid: {exc}"
        ) from exc
    if (
        execution_env.canonical_json_bytes(final_projection)
        != execution_env.canonical_json_bytes(expected_projection)
    ):
        difference = _first_projection_difference(
            expected_projection, final_projection
        )
        raise ReplayExecutionError(
            "live execution environment changed during replay"
            + (f" at {difference}" if difference else "")
        )

    _verify_code_closure(context, reducer_files)
    _verify_outputs(context, tuple(completed))
    _verify_scratch(context, tuple(completed))
    if _output_state(context, tuple(completed)) != prior_output_state:
        raise ReplayExecutionError(
            "final execution-environment probe modified replay output"
        )

    return ReplayResult(
        generation_id=context.generation_id,
        isolation=isolation.name,
        stages=tuple(completed),
    )


class _JsonArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        raise ReplayExecutionError(f"arguments: {message}")


def _parser() -> argparse.ArgumentParser:
    parser = _JsonArgumentParser(description=__doc__)
    parser.add_argument("--context", required=True, type=Path)
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--root", type=Path)
    parser.add_argument("--expected-generation-id", required=True)
    parser.add_argument("--python", type=Path, default=Path(sys.executable))
    parser.add_argument(
        "--stage-timeout-seconds",
        type=float,
        default=DEFAULT_STAGE_TIMEOUT_SECONDS,
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    try:
        require_isolated_startup()
        args = _parser().parse_args(argv)
        manifest = args.manifest.resolve(strict=True)
        pack_root = (args.root or manifest.parent).resolve(strict=True)
        document, _raw = contracts.load_canonical_json(manifest)
        pack = contracts.validate_offline_pack(document)
        if pack["schema"] not in {
            contracts.PACK_SCHEMA_V2,
            contracts.PACK_SCHEMA_V3,
        }:
            raise ReplayExecutionError(
                "run_replay_v2 requires offline-pack/v2 or offline-pack/v3"
            )
        contracts.verify_offline_pack_files(
            pack, pack_root, manifest_path=manifest
        )
        context = build_context.BuildContext.load(args.context)
        if context.replay.offline_pack_id != pack["offline_pack_id"]:
            raise ReplayExecutionError(
                "build context offline_pack_id does not match the verified pack"
            )
        reducer_files = tuple(
            (item["logical_path"], item["bytes"], item["sha256"])
            for item in pack["reducer"]["files"]
        )
        result = run_replay_v2(
            args.context,
            reducer_files=reducer_files,
            expected_generation_id=args.expected_generation_id,
            expected_offline_pack_id=pack["offline_pack_id"],
            expected_source_set_root=pack["source_set_root"],
            expected_reducer_inventory_id=pack["inventory"]["inventory_id"],
            expected_reducer_git_commit=pack["reducer"]["git_commit"],
            expected_configuration_sha256=pack["configuration"]["sha256"],
            expected_environment_sha256=pack["environment"]["sha256"],
            interpreter=args.python,
            stage_timeout_seconds=args.stage_timeout_seconds,
        )
    except (
        OSError,
        ReplayExecutionError,
        build_context.BuildContextError,
        contracts.VerificationError,
    ) as exc:
        print(
            build_context.canonical_json_bytes(
                {"error": {"message": str(exc), "type": type(exc).__name__}, "ok": False}
            ).decode("utf-8"),
            file=sys.stderr,
        )
        return 1
    print(build_context.canonical_json_bytes(result.to_document()).decode("utf-8"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
