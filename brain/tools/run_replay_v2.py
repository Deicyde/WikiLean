#!/usr/bin/env python3
"""Execute every stage of one freshly prepared Brain replay workspace.

The pack verifier/materializer is responsible for constructing the workspace.
This runner validates that boundary, executes the sealed stage schedule exactly
once and in order, and requires an operating-system sandbox for every reducer
subprocess.  It has no flag that disables isolation.
"""
from __future__ import annotations

import os
import sys
import argparse
import hashlib
import json
import stat
import subprocess
import sysconfig
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any


HERE = Path(__file__).resolve().parent
BRAIN = HERE.parent
for module_root in (BRAIN, HERE):
    if str(module_root) not in sys.path:
        sys.path.insert(0, str(module_root))

import build_context  # noqa: E402
import authority_contracts as contracts  # noqa: E402


class ReplayExecutionError(RuntimeError):
    """A prepared replay cannot be executed safely or completely."""


@dataclass(frozen=True, slots=True)
class IsolationBoundary:
    name: str
    prefix: tuple[str, ...]


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
ReducerFileSpec = tuple[str, int, str]
OutputState = tuple[int, int, int, int, int, int, str | None]
EXECUTION_ENVIRONMENT_NAME = "execution-environment.json"

_RUN_STAGE = (
    "import os,runpy,sys;"
    "program=sys.argv.pop(1);"
    "sys.path.insert(0,os.path.dirname(program));"
    "sys.argv[0]=program;"
    "runpy.run_path(program,run_name='__main__')"
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
) -> tuple[Path, tuple[Path, ...]]:
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
    _verify_execution_environment(workspace, context)
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
    return workspace, tuple(programs)


def _scheme_string(value: Path) -> str:
    return json.dumps(str(value), ensure_ascii=True)


def _runtime_roots(interpreter: Path) -> tuple[Path, ...]:
    """Return the host runtime trees required by this runner's interpreter."""
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
    configured = sysconfig.get_paths()
    for key in ("purelib", "platlib"):
        item = configured.get(key)
        if not item:
            continue
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
    for root in roots:
        _real_directory(root, "Python runtime root")
    return tuple(roots)


def _runtime_pythonpath(interpreter: Path) -> str:
    roots = _runtime_roots(interpreter)
    return os.pathsep.join(
        str(root)
        for root in roots
        if root.name in {"site-packages", "dist-packages"}
    )


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
    return IsolationBoundary("darwin-sandbox-exec", (str(executable), "-p", profile))


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
        "HOME": "/nonexistent",
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "PATH": "/nonexistent",
        "PYTHONPATH": _runtime_pythonpath(interpreter),
        "PYTHONHASHSEED": "0",
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONNOUSERSITE": "1",
        "TZ": "UTC",
        "WIKILEAN_OFFLINE": "1",
    }


def _execute(
    command: tuple[str, ...],
    cwd: Path,
    environment: Mapping[str, str],
    isolation: IsolationBoundary,
) -> int:
    process = subprocess.run(
        [*isolation.prefix, *command],
        cwd=cwd,
        env=dict(environment),
        check=False,
    )
    return process.returncode


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
    _executor: Executor | None = None,
    _isolation: IsolationBoundary | None = None,
) -> ReplayResult:
    """Run a prepared replay. Test seams are private and unavailable from the CLI."""
    require_isolated_startup()
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
    _workspace, programs = _validate_workspace(source, context, reducer_files)
    requested_python = Path(os.path.abspath(os.fspath(interpreter)))
    resolved_python = requested_python.resolve(strict=True)
    _real_file(resolved_python, "Python interpreter")
    _runtime_roots(resolved_python)
    interpreter_state = _hash_file(resolved_python)
    isolation = _isolation or _select_isolation(context, resolved_python)
    executor = _executor or _execute
    environment = _environment(resolved_python)
    completed: list[str] = []
    prior_output_state: dict[str, OutputState] = {}

    for stage, program in zip(context.stages, programs):
        command = (
            str(resolved_python),
            "-P",
            "-S",
            "-s",
            "-B",
            "-c",
            _RUN_STAGE,
            str(program),
            *stage.argv,
            "--build-context",
            str(source),
            "--stage-id",
            stage.id,
        )
        returncode = executor(command, context.roots.code, environment, isolation)
        if returncode != 0:
            raise ReplayExecutionError(
                f"stage {stage.id!r} failed with exit status {returncode}"
            )
        completed.append(stage.id)
        completed_ids = tuple(completed)
        if _hash_file(resolved_python) != interpreter_state:
            raise ReplayExecutionError("Python interpreter changed during replay")
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
    return parser


def main(argv: list[str] | None = None) -> int:
    try:
        require_isolated_startup()
        args = _parser().parse_args(argv)
        manifest = args.manifest.resolve(strict=True)
        pack_root = (args.root or manifest.parent).resolve(strict=True)
        document, _raw = contracts.load_canonical_json(manifest)
        pack = contracts.validate_offline_pack(document)
        if pack["schema"] != contracts.PACK_SCHEMA_V2:
            raise ReplayExecutionError("run_replay_v2 requires offline-pack/v2")
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
