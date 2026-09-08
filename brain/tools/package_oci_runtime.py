#!/usr/bin/env python3
"""Freeze a network-free OCI build context from a pinned Git runner and NumPy wheel.

The reviewed base image must already contain CPython 3.12, pip, and unprivileged
bubblewrap. This command does not acquire dependencies or invoke a build engine.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import sys
import tempfile
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import execution_environment as environment
import launch_replay_oci as launcher
import oci_runtime
import run_replay_v2 as runner
import prepare_replay_v2

BASE_RE = re.compile(r"[a-z0-9][a-z0-9./:_-]*@sha256:[0-9a-f]{64}")


def _git_output(git: Path, repo: Path, arguments: list[str]) -> bytes:
    status, stdout, stderr = launcher._bounded_process(
        [str(git), "--no-replace-objects", "-c", "protocol.allow=never", "-C", str(repo), *arguments],
        env={"PATH": "/nonexistent", "HOME": "/nonexistent", "GIT_CONFIG_NOSYSTEM": "1",
             "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_NO_LAZY_FETCH": "1",
             "GIT_ALLOW_PROTOCOL": "",
             "GIT_NO_REPLACE_OBJECTS": "1", "GIT_OPTIONAL_LOCKS": "0",
             "GIT_TERMINAL_PROMPT": "0", "LANG": "C", "LC_ALL": "C"},
        cwd=repo, timeout=30,
    )
    if status != 0 or stderr:
        raise ValueError("cannot read exact committed runner from offline Git repository")
    return stdout


def _preflight_git(git: Path, repo: Path, commit: str) -> None:
    # Reject lazy-fetch configuration before any object access. Include local
    # include files and worktree configuration; system/global config is disabled
    # by _git_output. GIT_NO_LAZY_FETCH remains set on every subsequent read.
    raw = _git_output(git, repo, ["config", "--includes", "--null", "--list"])
    for record in raw.split(b"\0"):
        if not record:
            continue
        key, separator, _value = record.partition(b"\n")
        if not separator:
            raise ValueError("Git returned malformed configuration")
        name = key.decode("utf-8", "strict").lower()
        if name == "extensions.partialclone" or (
            name.startswith("remote.") and name.endswith((".promisor", ".partialclonefilter"))
        ):
            raise ValueError("promisor and partial-clone repositories are forbidden for offline packaging")
    top = _git_output(git, repo, ["rev-parse", "--show-toplevel"]).decode("utf-8", "strict").strip()
    if Path(top).resolve(strict=True) != repo:
        raise ValueError("runner repository must be the Git worktree top level")
    actual = _git_output(git, repo, ["rev-parse", "--verify", commit + "^{commit}"]).decode("ascii", "strict").strip()
    if actual != commit:
        raise ValueError("Git did not resolve the exact runner commit")


def dockerfile(base_image: str, policy: dict[str, Any]) -> str:
    if BASE_RE.fullmatch(base_image) is None:
        raise ValueError("base image must be an explicit repository@sha256 digest")
    policy = oci_runtime.validate_policy(policy)
    digest = hashlib.sha256(environment.canonical_json_bytes(policy)).hexdigest()
    # No Dockerfile frontend tag is fetched; the installed builder is responsible
    # for supporting RUN --network=none and must also run with --network=none.
    return "\n".join([
        "FROM " + base_image,
        "USER 0:0",
        "COPY wheels/ /opt/wikilean-runtime/wheels/",
        "COPY requirements.txt runtime-policy.json /opt/wikilean-runtime/",
        "RUN --network=none /usr/local/bin/python3.12 -I -m pip --isolated install --force-reinstall --no-compile --no-cache-dir --no-index --no-deps --require-hashes --find-links=/opt/wikilean-runtime/wheels -r /opt/wikilean-runtime/requirements.txt",
        "COPY runner/ /opt/wikilean/",
        "RUN --network=none /usr/local/bin/python3.12 -I -B -c \"import pathlib,sys; assert sys.version_info[:2] == (3,12); assert pathlib.Path('/usr/bin/bwrap').is_file()\"",
        "LABEL " + oci_runtime.POLICY_LABEL + "=" + digest,
        "WORKDIR /opt/wikilean",
        'ENTRYPOINT ["/usr/local/bin/python3.12","-I","-B","/opt/wikilean/brain/tools/oci_replay_entrypoint.py"]',
        "",
    ])


def package(*, git: Path, repo: Path, commit: str, base_image: str,
            policy_path: Path, wheelhouse: Path, destination: Path) -> dict[str, Any]:
    if environment.GIT_COMMIT_RE.fullmatch(commit) is None:
        raise ValueError("runner commit must be a full immutable Git commit")
    git = launcher._real_path(git, directory=False)
    repo = launcher._real_path(repo, directory=True)
    destination = destination.absolute()
    launcher._real_path(destination.parent, directory=True)
    if destination.exists() or destination.is_symlink():
        raise ValueError("OCI packaging destination must be fresh")
    policy, raw = oci_runtime.read_control(policy_path)
    if raw != environment.canonical_json_bytes(policy):
        raise ValueError("runtime policy must use canonical JSON")
    docker_bytes = dockerfile(base_image, policy).encode()
    wheel = wheelhouse / policy["numpy"]["wheel"]
    expected = (policy["numpy"]["sha256"], policy["numpy"]["bytes"])
    if environment.secure_file_digest(wheel) != expected:
        raise ValueError("immutable NumPy artifact mismatch")
    apparmor_binary = (oci_runtime.apparmor_runtime.verify_artifact(policy["apparmor"], wheelhouse)
                       if "apparmor" in policy else None)
    _preflight_git(git, repo, commit)
    code: dict[str, bytes] = {}
    for logical in sorted(runner.RUNNER_FILES):
        code[logical] = _git_output(git, repo, ["cat-file", "blob", commit + ":" + logical])
    inventory = [{"path": path, "sha256": hashlib.sha256(data).hexdigest(), "bytes": len(data)} for path, data in sorted(code.items())]
    record = {"schema": "wikilean.oci-build-context/v1", "runner_git_commit": commit,
              "base_image": base_image, "policy_sha256": hashlib.sha256(raw).hexdigest(), "runner_files": inventory,
              "wheel": policy["numpy"], "dockerfile_sha256": hashlib.sha256(docker_bytes).hexdigest()}
    temporary = Path(tempfile.mkdtemp(prefix=".oci-package-", dir=destination.parent))
    try:
        (temporary / "wheels").mkdir()
        shutil.copyfile(wheel, temporary / "wheels" / wheel.name, follow_symlinks=False)
        if environment.secure_file_digest(temporary / "wheels" / wheel.name) != expected:
            raise ValueError("NumPy artifact changed during packaging")
        if apparmor_binary is not None:
            (temporary / "wheels" / policy["apparmor"]["binary"]).write_bytes(apparmor_binary)
        for logical, data in code.items():
            target = temporary / "runner" / logical
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
        (temporary / "runtime-policy.json").write_bytes(raw)
        (temporary / "Dockerfile").write_bytes(docker_bytes)
        requirement = f"numpy=={policy['numpy']['version']} --hash=sha256:{policy['numpy']['sha256']}\n"
        (temporary / "requirements.txt").write_text(requirement)
        (temporary / "build-context.json").write_bytes(environment.canonical_json_bytes(record))
        for directory, _names, filenames in os.walk(temporary, topdown=False):
            parent = Path(directory)
            for filename in filenames:
                path = parent / filename
                path.chmod(0o444)
                with path.open("rb") as stream:
                    os.fsync(stream.fileno())
            if parent != temporary:
                parent.chmod(0o555)
            prepare_replay_v2._fsync_directory(parent)
        prepare_replay_v2._publish_no_replace(temporary, destination)
        destination.chmod(0o555)
        prepare_replay_v2._fsync_directory(destination.parent)
    except BaseException:
        if temporary.exists():
            for directory, _names, _filenames in os.walk(temporary):
                Path(directory).chmod(0o700)
            shutil.rmtree(temporary)
        raise
    return record


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("git", "repo", "policy", "wheelhouse", "destination"):
        parser.add_argument("--" + name, type=Path, required=True)
    parser.add_argument("--commit", required=True)
    parser.add_argument("--base-image", required=True)
    args = parser.parse_args(argv)
    try:
        result = package(git=args.git, repo=args.repo, commit=args.commit, base_image=args.base_image,
                         policy_path=args.policy, wheelhouse=args.wheelhouse, destination=args.destination)
    except (OSError, ValueError, RuntimeError) as exc:
        print(environment.canonical_json_bytes({"ok": False, "error": str(exc)}).decode(), file=sys.stderr)
        return 1
    print(environment.canonical_json_bytes(result).decode())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
