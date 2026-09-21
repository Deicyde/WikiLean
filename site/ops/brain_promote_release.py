#!/usr/bin/env python3
"""Promote one verified, immutable Brain release without rebuilding it.

Production mutation is deliberately separated from the nightly reducer.  The
promoter stages a fresh external public tree, verifies the clean Git authority,
records a durable intent, invokes Wrangler once, and qualifies the result with
release-aware control-plane and content canaries.
"""
from __future__ import annotations

import argparse
import contextlib
import copy
import fcntl
import hashlib
import json
import math
import os
import re
import signal
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from brain_deploy_journal import (
    EventJournal,
    JournalError,
    PromotionLock,
    list_incomplete_attempts,
    validate_target_receipt_root,
)
from brain_http import SelectorProbe, TransportError, probe_selector, require_https_base_url
from brain_public_baseline import (
    BaselineFreezeError,
    BaselineValidationError,
    PublicAssetBaseline,
    _publish_no_replace,
    _remove_owned_directory_at,
    _verify_directory_at,
    _verify_store_identity,
    verify_public_baseline,
)


RELEASE_ID_RE = re.compile(r"^sha256:([0-9a-f]{64})$")
GIT_COMMIT_RE = re.compile(r"^[0-9a-f]{40}$")
UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
)
ATTEMPT_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
SELECTOR_SCHEMA_V1 = "wikilean.release-selector/v1"
SELECTOR_SCHEMA = "wikilean.release-selector/v2"
RELEASE_SCHEMA = "wikilean.release/v1"
DRY_RUN_SCHEMA = "wikilean.brain-promotion-dry-run/v2"
DRY_RUN_ARTIFACT_SCHEMA = "wikilean.brain-promotion-dry-run-artifacts/v1"
DRY_RUN_ARTIFACT_DOMAIN = "wikilean.brain-promotion-dry-run-artifacts.v1"
RESULT_SCHEMA = "wikilean.brain-promotion-result/v1"
MAX_COMMAND_EVIDENCE_BYTES = 4 * 1024 * 1024
MAX_NODE_MODULE_OBJECTS = 100_000
MAX_NODE_MODULE_BYTES = 2 * 1024 * 1024 * 1024
PRODUCTION_ORIGIN = "https://wikilean.jackmccarthy.org"


class PromotionError(RuntimeError):
    """A fail-closed promotion or reconciliation error."""


@dataclass(frozen=True)
class RunResult:
    args: tuple[str, ...]
    returncode: int | None
    stdout: bytes
    stderr: bytes
    timed_out: bool = False

    @property
    def ok(self) -> bool:
        return self.returncode == 0 and not self.timed_out


class CommandRunner:
    @staticmethod
    def _terminate_process_group(process: subprocess.Popen[bytes]) -> tuple[bytes, bytes]:
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        try:
            return process.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            return process.communicate()

    def run(
        self,
        args: Sequence[str],
        *,
        cwd: Path,
        timeout: float | None = None,
        env: Mapping[str, str] | None = None,
    ) -> RunResult:
        command = tuple(str(value) for value in args)
        process = subprocess.Popen(
            command,
            cwd=cwd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
            env=env,
        )
        try:
            stdout, stderr = process.communicate(timeout=timeout)
            return RunResult(command, process.returncode, stdout, stderr)
        except subprocess.TimeoutExpired:
            # npx may spawn a Node child. Kill the whole private process group so
            # a timed-out deploy cannot outlive the recorded command result and
            # commit remotely during later reconciliation.
            stdout, stderr = self._terminate_process_group(process)
            return RunResult(command, None, stdout, stderr, timed_out=True)
        except BaseException:
            # start_new_session detaches the child from terminal signals. Always
            # tear down that private process group before propagating Ctrl-C or
            # any other interruption; otherwise Wrangler could mutate after the
            # promoter releases its lock and deletes the sealed workspace.
            try:
                self._terminate_process_group(process)
            except BaseException:
                pass
            raise


@dataclass(frozen=True)
class ReleaseInfo:
    release_id: str
    release_hex: str
    root: Path
    manifest: Path
    manifest_sha256: str
    authority_commit: str
    reducer_commit: str
    tree: dict[str, object]


@dataclass(frozen=True)
class SelectorState:
    status: int
    body_sha256: str
    body: bytes
    current_release_id: str | None
    previous_release_id: str | None
    retained_release_id: str | None
    audited_at: str | None
    current_manifest_sha256: str | None = None
    previous_manifest_sha256: str | None = None
    retained_manifest_sha256: str | None = None


@dataclass(frozen=True)
class DeploymentState:
    deployment_id: str
    version_id: str
    raw_sha256: str


@dataclass(frozen=True)
class PreparedPromotion:
    attempt_id: str
    audited_at: str
    tag: str
    message: str
    candidate: ReleaseInfo
    prior: ReleaseInfo | None
    predeploy_release: ReleaseInfo | None
    public_baseline: PublicAssetBaseline
    initial_selector: SelectorState
    predeploy: DeploymentState
    predeploy_status_before: bytes
    predeploy_status_after: bytes
    public_dir: Path
    public_inventory: dict[str, object]
    public_result: dict[str, object]
    staged_selector: SelectorState
    bundle_dir: Path
    bundle_entry: Path
    bundle_inventory: dict[str, object]
    deploy_config: Path
    deploy_config_sha256: str
    node_version: str
    wrangler_version: str
    trust_source: str
    history: dict[str, object]
    history_raw: Mapping[str, bytes] | None = None
    activation: ReviewedActivation | None = None
    wrangler_installation: Mapping[str, object] | None = None
    node_executables: Mapping[str, object] | None = None


@dataclass(frozen=True)
class RetainedDryRunArtifacts:
    artifact_id: str
    artifact_hex: str
    root: Path
    manifest: Path
    manifest_sha256: str
    public_dir: Path
    worker_dir: Path
    worker_entry: Path
    config: Path

    def reference(self) -> dict[str, str]:
        return {
            "schema": DRY_RUN_ARTIFACT_SCHEMA,
            "artifact_id": self.artifact_id,
            "root": str(self.root),
            "manifest": str(self.manifest),
            "manifest_sha256": self.manifest_sha256,
        }


@dataclass(frozen=True)
class ReviewedActivation:
    root: Path
    manifest: Path
    manifest_sha256: str
    bundle_id: str
    semantic_baseline_release_id: str
    release_id: str
    baseline_id: str
    authority_commit: str
    reducer_commit: str
    dry_run_attempt_id: str
    intent: Mapping[str, object]
    retained: RetainedDryRunArtifacts

    def reference(self) -> dict[str, str]:
        return {
            "bundle_id": self.bundle_id,
            "root": str(self.root),
            "manifest": str(self.manifest),
            "manifest_sha256": self.manifest_sha256,
            "semantic_baseline_release_id": self.semantic_baseline_release_id,
            "retained_artifact_id": self.retained.artifact_id,
            "retained_artifact_root": str(self.retained.root),
            "retained_artifact_manifest_sha256": self.retained.manifest_sha256,
        }


@dataclass(frozen=True)
class ReconciliationObservation:
    deployment: DeploymentState
    selector_probe: SelectorProbe
    selector: SelectorState | None
    annotations: dict[str, str]
    trust_source: str
    status_before: bytes
    status_after: bytes


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def canonical_json_bytes(value: object) -> bytes:
    return (
        json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
        + "\n"
    ).encode("utf-8")


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def journal_safe(value: object) -> object:
    """Map operational metrics into the journal's integer/string JSON profile."""
    if isinstance(value, float):
        if value != value or value in {float("inf"), float("-inf")}:
            raise PromotionError("journal evidence contains a non-finite number")
        return format(value, ".17g")
    if isinstance(value, list):
        return [journal_safe(item) for item in value]
    if isinstance(value, tuple):
        return [journal_safe(item) for item in value]
    if isinstance(value, dict):
        return {str(key): journal_safe(item) for key, item in value.items()}
    return value


def append_event(
    journal: EventJournal,
    kind: str,
    payload: Mapping[str, object],
) -> dict[str, object]:
    safe = journal_safe(dict(payload))
    assert isinstance(safe, dict)
    return journal.append(kind, safe)


def _object_without_duplicates(
    pairs: list[tuple[str, object]],
    *,
    label: str,
) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise PromotionError(f"{label} contains duplicate JSON key {key!r}")
        result[key] = value
    return result


def _decode_json(text: bytes, label: str) -> object:
    try:
        return json.loads(
            text,
            object_pairs_hook=lambda pairs: _object_without_duplicates(pairs, label=label),
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PromotionError(f"{label} is not valid JSON: {exc}") from exc


def extract_last_json_value(output: bytes, label: str) -> object:
    text = output.decode("utf-8", errors="replace")
    decoder = json.JSONDecoder(
        object_pairs_hook=lambda pairs: _object_without_duplicates(pairs, label=label)
    )
    matches: list[tuple[int, int, object]] = []
    for index, char in enumerate(text):
        if char not in "[{":
            continue
        try:
            value, length = decoder.raw_decode(text[index:])
        except json.JSONDecodeError:
            continue
        matches.append((index, index + length, value))
    if not matches:
        raise PromotionError(f"{label} emitted no JSON value")
    top_level = [
        match
        for match in matches
        if not any(
            other_start < match[0] and other_end >= match[1]
            for other_start, other_end, _ in matches
        )
    ]
    return max(top_level, key=lambda match: match[0])[2]


def _is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def inventory_tree(root: Path) -> dict[str, object]:
    resolved = root.resolve(strict=True)
    if root.is_symlink() or resolved != root.absolute():
        raise PromotionError(f"tree root must not use symlink aliases: {root}")
    digest = hashlib.sha256()
    digest.update(b"wikilean\0wikilean.file-tree.v1\0")
    objects = 0
    byte_count = 0
    for path in sorted(resolved.rglob("*"), key=lambda value: value.relative_to(resolved).as_posix()):
        relative = path.relative_to(resolved).as_posix()
        if path.is_symlink():
            raise PromotionError(f"tree contains a symlink: {relative}")
        if path.is_dir():
            continue
        if not path.is_file():
            raise PromotionError(f"tree contains a non-regular entry: {relative}")
        body = path.read_bytes()
        file_digest = sha256_bytes(body)
        encoded = relative.encode("utf-8")
        digest.update(len(encoded).to_bytes(4, "big"))
        digest.update(encoded)
        digest.update(len(body).to_bytes(8, "big"))
        digest.update(bytes.fromhex(file_digest))
        objects += 1
        byte_count += len(body)
    return {
        "schema": "wikilean.file-tree-inventory/v1",
        "root": str(resolved),
        "objects": objects,
        "bytes": byte_count,
        "sha256": digest.hexdigest(),
    }


def inventory_node_modules(wiki_root: Path) -> dict[str, object]:
    """Fingerprint the installed Node tree without following symlinks."""
    root_input = wiki_root / "node_modules"
    lockfile = wiki_root / "package-lock.json"
    if root_input.is_symlink() or not root_input.is_dir():
        raise PromotionError("wiki/node_modules must be a physical directory")
    root = root_input.resolve(strict=True)
    if root != root_input.absolute():
        raise PromotionError("wiki/node_modules must not traverse symlink aliases")
    if lockfile.is_symlink() or not lockfile.is_file():
        raise PromotionError("wiki/package-lock.json must be a regular file")
    lockfile_body = lockfile.read_bytes()
    digest = hashlib.sha256()
    digest.update(b"wikilean\0wikilean.node-modules-inventory.v1\0")
    digest.update(hashlib.sha256(lockfile_body).digest())
    objects = 0
    byte_count = 0
    symlinks = 0
    hard_links: dict[tuple[int, int], tuple[int, list[str]]] = {}
    pending = [root]
    while pending:
        directory = pending.pop()
        try:
            entries = sorted(os.scandir(directory), key=lambda entry: entry.name)
        except OSError as exc:
            raise PromotionError(f"cannot scan installed Node dependency tree: {exc}") from exc
        for entry in entries:
            path = Path(entry.path)
            relative = path.relative_to(root).as_posix()
            info = entry.stat(follow_symlinks=False)
            if stat.S_ISDIR(info.st_mode):
                pending.append(path)
                continue
            if stat.S_ISLNK(info.st_mode):
                target = os.readlink(path)
                if os.path.isabs(target):
                    raise PromotionError(
                        f"installed Node dependency uses an absolute symlink: {relative}"
                    )
                try:
                    resolved_target = (path.parent / target).resolve(strict=True)
                except OSError as exc:
                    raise PromotionError(
                        f"installed Node dependency symlink is broken: {relative}"
                    ) from exc
                if (
                    not _is_relative_to(resolved_target, root)
                    or not resolved_target.is_file()
                ):
                    raise PromotionError(
                        f"installed Node dependency symlink escapes its tree: {relative}"
                    )
                body = target.encode("utf-8")
                kind = b"L"
                symlinks += 1
            elif stat.S_ISREG(info.st_mode):
                flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
                descriptor = os.open(path, flags)
                try:
                    before = os.fstat(descriptor)
                    file_digest = hashlib.sha256()
                    size = 0
                    while True:
                        chunk = os.read(descriptor, 1024 * 1024)
                        if not chunk:
                            break
                        size += len(chunk)
                        byte_count += len(chunk)
                        if byte_count > MAX_NODE_MODULE_BYTES:
                            raise PromotionError(
                                "installed Node dependency tree exceeds the byte limit"
                            )
                        file_digest.update(chunk)
                    after = os.fstat(descriptor)
                finally:
                    os.close(descriptor)
                if (
                    before.st_dev,
                    before.st_ino,
                    before.st_mode,
                    before.st_nlink,
                    before.st_size,
                    before.st_mtime_ns,
                    before.st_ctime_ns,
                ) != (
                    after.st_dev,
                    after.st_ino,
                    after.st_mode,
                    after.st_nlink,
                    after.st_size,
                    after.st_mtime_ns,
                    after.st_ctime_ns,
                ) or size != before.st_size:
                    raise PromotionError(
                        f"installed Node dependency changed while hashing: {relative}"
                    )
                body = file_digest.digest()
                record_size = size
                kind = b"F"
                link_key = (after.st_dev, after.st_ino)
                expected_links, paths = hard_links.setdefault(
                    link_key, (after.st_nlink, [])
                )
                if expected_links != after.st_nlink:
                    raise PromotionError(
                        f"installed Node dependency link count changed: {relative}"
                    )
                paths.append(relative)
            else:
                raise PromotionError(
                    f"installed Node dependency tree contains an unsafe entry: {relative}"
                )
            encoded = relative.encode("utf-8")
            digest.update(kind)
            digest.update(len(encoded).to_bytes(4, "big"))
            digest.update(encoded)
            if kind == b"L":
                record_size = len(body)
            digest.update(record_size.to_bytes(8, "big"))
            digest.update(body)
            objects += 1
            if objects > MAX_NODE_MODULE_OBJECTS:
                raise PromotionError(
                    "installed Node dependency tree exceeds the object limit"
                )
    for expected_links, paths in hard_links.values():
        if expected_links != len(paths):
            raise PromotionError(
                "installed Node dependency has a hard link outside node_modules: "
                + paths[0]
            )
    return {
        "schema": "wikilean.node-modules-inventory/v1",
        "root": str(root),
        "package_lock_sha256": sha256_bytes(lockfile_body),
        "objects": objects,
        "bytes": byte_count,
        "symlinks": symlinks,
        "sha256": digest.hexdigest(),
    }


def _resolved_executable(name: str) -> Path:
    selected = shutil.which(name)
    if selected is None:
        raise PromotionError(f"required executable is unavailable: {name}")
    invocation = Path(selected).absolute()
    try:
        resolved = invocation.resolve(strict=True)
    except OSError as exc:
        raise PromotionError(f"cannot resolve {name} executable: {exc}") from exc
    if not resolved.is_file() or not os.access(resolved, os.X_OK):
        raise PromotionError(f"{name} executable is not an executable regular file")
    return resolved


def _executable_identity(name: str) -> dict[str, object]:
    resolved = _resolved_executable(name)
    body = resolved.read_bytes()
    return {
        "path": str(resolved),
        "sha256": sha256_bytes(body),
        "bytes": len(body),
    }


def _node_environment(
    node: Path | None = None,
    *,
    include_cloudflare: bool = False,
) -> dict[str, str]:
    allowed = {
        "HOME",
        "TMPDIR",
        "TMP",
        "TEMP",
        "LANG",
        "LC_ALL",
        "SSL_CERT_FILE",
        "SSL_CERT_DIR",
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "NO_PROXY",
        "http_proxy",
        "https_proxy",
        "no_proxy",
        "CI",
    }
    if include_cloudflare:
        allowed.update({"CLOUDFLARE_API_TOKEN", "CLOUDFLARE_ACCOUNT_ID"})
    environment = {name: value for name, value in os.environ.items() if name in allowed}
    node_path = node or _resolved_executable("node")
    environment["PATH"] = os.pathsep.join((str(node_path.parent), os.defpath))
    return environment


def seal_tree_read_only(root: Path) -> None:
    """Remove write permission from an isolated staged tree before Wrangler reads it."""
    resolved = root.resolve(strict=True)
    for path in sorted(resolved.rglob("*"), key=lambda value: len(value.parts), reverse=True):
        if path.is_symlink():
            raise PromotionError(f"cannot seal a tree containing a symlink: {path}")
        os.chmod(path, 0o500 if path.is_dir() else 0o400)
    os.chmod(resolved, 0o500)


def remove_sealed_tree(root: Path) -> None:
    if not root.exists():
        return
    for path in sorted(root.rglob("*"), key=lambda value: len(value.parts)):
        if path.is_dir() and not path.is_symlink():
            try:
                os.chmod(path, 0o700)
            except OSError:
                pass
        elif not path.is_symlink():
            try:
                os.chmod(path, 0o600)
            except OSError:
                pass
    try:
        os.chmod(root, 0o700)
    except OSError:
        pass
    shutil.rmtree(root, ignore_errors=True)


_DRY_RUN_EVIDENCE_PATHS = {
    "initial_selector": "evidence/initial-selector.body",
    "status_before": "evidence/status-before.body",
    "status_after": "evidence/status-after.body",
    "deployments_history": "evidence/deployments-history.body",
    "versions_history": "evidence/versions-history.body",
}


def _relative_tree_inventory(root: Path, relative: str) -> dict[str, object]:
    inventory = inventory_tree(root)
    return {
        "schema": inventory["schema"],
        "path": relative,
        "directories": _tree_directories(root),
        "objects": inventory["objects"],
        "bytes": inventory["bytes"],
        "sha256": inventory["sha256"],
    }


def _tree_directories(root: Path) -> list[str]:
    resolved = root.resolve(strict=True)
    directories: list[str] = []
    for path in sorted(
        resolved.rglob("*"), key=lambda value: value.relative_to(resolved).as_posix()
    ):
        relative = path.relative_to(resolved).as_posix()
        if path.is_symlink():
            raise PromotionError(f"tree contains a symlink: {relative}")
        if path.is_dir():
            directories.append(relative)
        elif not path.is_file():
            raise PromotionError(f"tree contains a non-regular entry: {relative}")
    return directories


def _file_evidence(path: str, body: bytes) -> dict[str, object]:
    return {"path": path, "bytes": len(body), "sha256": sha256_bytes(body)}


def _dry_run_artifact_id(identity: Mapping[str, object]) -> str:
    payload = (
        b"wikilean\0"
        + DRY_RUN_ARTIFACT_DOMAIN.encode("ascii")
        + b"\0canonical-json-v1\0"
        + canonical_json_bytes(identity).removesuffix(b"\n")
    )
    return "sha256:" + sha256_bytes(payload)


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _write_exclusive(path: Path, body: bytes) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    descriptor = os.open(
        path,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
        0o600,
    )
    try:
        view = memoryview(body)
        while view:
            written = os.write(descriptor, view)
            if written <= 0:
                raise PromotionError(f"short write while retaining {path.name}")
            view = view[written:]
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _copy_sealed_tree(source: Path, destination: Path, expected: Mapping[str, object]) -> None:
    before = inventory_tree(source)
    directories_before = _tree_directories(source)
    if before != dict(expected):
        raise PromotionError("sealed source tree changed before dry-run retention")
    shutil.copytree(source, destination, symlinks=True, copy_function=shutil.copyfile)
    after = inventory_tree(source)
    copied = inventory_tree(destination)
    directories_after = _tree_directories(source)
    directories_copied = _tree_directories(destination)
    if after != before or directories_after != directories_before:
        raise PromotionError("sealed source tree changed while retaining dry-run artifacts")
    if (
        any(copied[key] != before[key] for key in ("objects", "bytes", "sha256"))
        or directories_copied != directories_before
    ):
        raise PromotionError("retained dry-run tree differs from its sealed source")


def _seal_retained_tree(root: Path) -> None:
    entries = sorted(root.rglob("*"), key=lambda value: len(value.parts), reverse=True)
    for path in entries:
        info = path.lstat()
        if path.is_symlink() or (not path.is_file() and not path.is_dir()):
            raise PromotionError(f"retained dry-run artifact contains an unsafe entry: {path}")
        if path.is_file():
            if info.st_nlink != 1:
                raise PromotionError(f"retained dry-run artifact is hard-linked: {path}")
            path.chmod(0o444)
            descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
        else:
            path.chmod(0o555)
            _fsync_directory(path)
    root.chmod(0o555)
    _fsync_directory(root)


def _remove_pending_tree(root: Path) -> None:
    try:
        root_info = root.lstat()
    except FileNotFoundError:
        return
    if stat.S_ISLNK(root_info.st_mode) or not stat.S_ISDIR(root_info.st_mode):
        with contextlib.suppress(OSError):
            root.unlink()
        return
    for path in sorted(root.rglob("*"), key=lambda value: len(value.parts)):
        info = path.lstat()
        if stat.S_ISDIR(info.st_mode):
            with contextlib.suppress(OSError):
                path.chmod(0o700)
        elif stat.S_ISREG(info.st_mode):
            with contextlib.suppress(OSError):
                path.chmod(0o600)
    with contextlib.suppress(OSError):
        root.chmod(0o700)
    shutil.rmtree(root, ignore_errors=True)


def _physical_path_is_within(path: Path, boundary: Path) -> bool:
    """Compare existing path components by identity, including case-folding filesystems."""
    current = path
    while True:
        if current.exists() or current.is_symlink():
            try:
                if os.path.samefile(current, boundary):
                    return True
            except OSError:
                pass
        parent = current.parent
        if parent == current:
            return False
        current = parent


def _assert_external_dry_run_store(store: Path, protected: Sequence[Path]) -> None:
    if store.is_symlink() or not store.is_dir():
        raise PromotionError("dry-run artifact store must be a real directory")
    info = store.stat()
    mode = stat.S_IMODE(info.st_mode)
    if (
        info.st_uid != os.geteuid()
        or mode & 0o077
        or (mode & 0o700) != 0o700
    ):
        raise PromotionError(
            "dry-run artifact store must be user-owned and private (mode 0700)"
        )
    for boundary in protected:
        physical = boundary.resolve(strict=True)
        if _physical_path_is_within(store, physical) or _physical_path_is_within(
            physical, store
        ):
            raise PromotionError(
                "dry-run artifact store must be outside checkout, release, baseline, "
                "receipt, and promotion workspace roots"
            )


def _prepare_dry_run_store(raw: Path, protected: Sequence[Path]) -> Path:
    if not raw.is_absolute():
        raise PromotionError("--retain-dry-run-store must be an absolute path")
    if raw.exists() or raw.is_symlink():
        if raw.is_symlink() or not raw.is_dir():
            raise PromotionError("dry-run artifact store must be a real directory")
        store = raw.resolve(strict=True)
        if store != raw:
            raise PromotionError("dry-run artifact store must use its physical path")
    else:
        parent = raw.parent.resolve(strict=True)
        store = parent / raw.name
        if store != raw:
            raise PromotionError("dry-run artifact store must use its physical path")
        for boundary in protected:
            physical = boundary.resolve(strict=True)
            if _physical_path_is_within(store, physical) or _physical_path_is_within(
                physical, store
            ):
                raise PromotionError(
                    "dry-run artifact store must be outside checkout, release, baseline, "
                    "receipt, and promotion workspace roots"
                )
        store.mkdir(mode=0o700)
        store.chmod(0o700)
        _fsync_directory(parent)
    _assert_external_dry_run_store(store, protected)
    return store


@contextlib.contextmanager
def _dry_run_store_lock(store: Path):
    path = store / ".retain.lock"
    descriptor = os.open(
        path,
        os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0),
        0o600,
    )
    try:
        info = os.fstat(descriptor)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.geteuid()
            or info.st_nlink != 1
        ):
            raise PromotionError(
                "dry-run artifact store lock must be a user-owned single-link regular file"
            )
        os.fchmod(descriptor, 0o600)
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
    finally:
        os.close(descriptor)


def _load_canonical_manifest(path: Path) -> dict[str, object]:
    raw = path.read_bytes()
    try:
        value = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=lambda pairs: _object_without_duplicates(
                pairs, label="retained dry-run artifact manifest"
            ),
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PromotionError(f"retained dry-run artifact manifest is invalid: {exc}") from exc
    if not isinstance(value, dict) or canonical_json_bytes(value) != raw:
        raise PromotionError("retained dry-run artifact manifest is not canonical JSON")
    return value


def verify_retained_dry_run_artifacts(
    root_input: Path,
    *,
    expected_artifact_id: str | None = None,
) -> RetainedDryRunArtifacts:
    if not root_input.is_absolute() or root_input.is_symlink():
        raise PromotionError("retained dry-run root must be an absolute non-symlink path")
    root = root_input.resolve(strict=True)
    if root != root_input or not root.is_dir() or stat.S_IMODE(root.stat().st_mode) != 0o555:
        raise PromotionError("retained dry-run root must be a physical 0555 directory")
    expected_top = {"manifest.json", "public", "worker", "wrangler.jsonc", "evidence"}
    if {path.name for path in root.iterdir()} != expected_top:
        raise PromotionError("retained dry-run root has an unexpected top-level closure")
    for name in ("public", "worker", "evidence"):
        if not (root / name).is_dir() or (root / name).is_symlink():
            raise PromotionError(f"retained dry-run {name} path must be a directory")
    for name in ("manifest.json", "wrangler.jsonc"):
        if not (root / name).is_file() or (root / name).is_symlink():
            raise PromotionError(f"retained dry-run {name} path must be a regular file")
    for path in root.rglob("*"):
        info = path.lstat()
        relative = path.relative_to(root).as_posix()
        if path.is_symlink():
            raise PromotionError(
                f"retained dry-run artifact contains a symlink: {relative}"
            )
        if not path.is_file() and not path.is_dir():
            raise PromotionError(f"retained dry-run artifact contains an unsafe entry: {relative}")
        expected_mode = 0o555 if path.is_dir() else 0o444
        if stat.S_IMODE(info.st_mode) != expected_mode:
            raise PromotionError(f"retained dry-run artifact has wrong mode: {relative}")
        if path.is_file() and info.st_nlink != 1:
            raise PromotionError(f"retained dry-run artifact is hard-linked: {relative}")

    manifest_path = root / "manifest.json"
    manifest = _load_canonical_manifest(manifest_path)
    required = {
        "schema",
        "artifact_id",
        "attempt_id",
        "release_id",
        "authority_git_commit",
        "public_tree",
        "worker_bundle",
        "wrangler_config",
        "evidence",
    }
    if set(manifest) != required or manifest.get("schema") != DRY_RUN_ARTIFACT_SCHEMA:
        raise PromotionError("retained dry-run artifact manifest fields/schema are invalid")
    artifact_id = manifest.get("artifact_id")
    match = RELEASE_ID_RE.fullmatch(str(artifact_id or ""))
    if match is None or root.name != match.group(1):
        raise PromotionError("retained dry-run artifact identity/root mismatch")
    if expected_artifact_id is not None and artifact_id != expected_artifact_id:
        raise PromotionError("retained dry-run artifact identity differs from the expected ID")
    if ATTEMPT_ID_RE.fullmatch(str(manifest.get("attempt_id") or "")) is None:
        raise PromotionError("retained dry-run artifact attempt identity is invalid")
    if RELEASE_ID_RE.fullmatch(str(manifest.get("release_id") or "")) is None:
        raise PromotionError("retained dry-run artifact release identity is invalid")
    if GIT_COMMIT_RE.fullmatch(str(manifest.get("authority_git_commit") or "")) is None:
        raise PromotionError("retained dry-run artifact authority identity is invalid")

    def validate_tree(raw: object, name: str, expected_path: str) -> dict[str, object]:
        if not isinstance(raw, dict) or set(raw) != {
            "schema", "path", "directories", "objects", "bytes", "sha256"
        }:
            raise PromotionError(f"retained {name} inventory fields are invalid")
        if raw.get("schema") != "wikilean.file-tree-inventory/v1" or raw.get("path") != expected_path:
            raise PromotionError(f"retained {name} inventory identity is invalid")
        if (
            isinstance(raw.get("objects"), bool)
            or not isinstance(raw.get("objects"), int)
            or raw["objects"] < 0
            or isinstance(raw.get("bytes"), bool)
            or not isinstance(raw.get("bytes"), int)
            or raw["bytes"] < 0
            or not isinstance(raw.get("sha256"), str)
            or re.fullmatch(r"[0-9a-f]{64}", raw["sha256"]) is None
        ):
            raise PromotionError(f"retained {name} inventory metrics are invalid")
        actual = _relative_tree_inventory(root / expected_path, expected_path)
        if raw != actual:
            raise PromotionError(f"retained {name} tree differs from its inventory")
        return raw

    validate_tree(manifest.get("public_tree"), "public", "public")
    worker = manifest.get("worker_bundle")
    if not isinstance(worker, dict) or set(worker) != {
        "schema", "path", "directories", "entry", "objects", "bytes", "sha256"
    }:
        raise PromotionError("retained worker bundle inventory fields are invalid")
    worker_tree = {
        key: worker[key]
        for key in ("schema", "path", "directories", "objects", "bytes", "sha256")
    }
    validate_tree(worker_tree, "worker", "worker")
    entry = worker.get("entry")
    if (
        not isinstance(entry, str)
        or not entry.startswith("worker/")
        or Path(entry).is_absolute()
        or ".." in Path(entry).parts
        or Path(entry).as_posix() != entry
    ):
        raise PromotionError("retained worker entry path is invalid")
    worker_entry = root / entry
    if (
        worker_entry.is_symlink()
        or not worker_entry.is_file()
        or not _is_relative_to(
            worker_entry.resolve(strict=True), (root / "worker").resolve(strict=True)
        )
    ):
        raise PromotionError("retained worker entry is missing")

    def validate_file(raw: object, label: str, expected_path: str) -> None:
        if not isinstance(raw, dict) or set(raw) != {"path", "bytes", "sha256"}:
            raise PromotionError(f"retained {label} evidence fields are invalid")
        if raw.get("path") != expected_path:
            raise PromotionError(f"retained {label} path is invalid")
        if (
            isinstance(raw.get("bytes"), bool)
            or not isinstance(raw.get("bytes"), int)
            or raw["bytes"] < 0
            or not isinstance(raw.get("sha256"), str)
            or re.fullmatch(r"[0-9a-f]{64}", raw["sha256"]) is None
        ):
            raise PromotionError(f"retained {label} evidence metrics are invalid")
        path = root / expected_path
        body = path.read_bytes()
        if raw.get("bytes") != len(body) or raw.get("sha256") != sha256_bytes(body):
            raise PromotionError(f"retained {label} bytes differ from the manifest")

    validate_file(manifest.get("wrangler_config"), "Wrangler config", "wrangler.jsonc")
    evidence = manifest.get("evidence")
    if not isinstance(evidence, dict) or set(evidence) != set(_DRY_RUN_EVIDENCE_PATHS):
        raise PromotionError("retained dry-run evidence closure is invalid")
    expected_evidence_files = set()
    for key, relative in _DRY_RUN_EVIDENCE_PATHS.items():
        validate_file(evidence[key], key, relative)
        expected_evidence_files.add(Path(relative).name)
    if {path.name for path in (root / "evidence").iterdir()} != expected_evidence_files:
        raise PromotionError("retained dry-run evidence directory has extra or missing files")

    identity = dict(manifest)
    identity.pop("artifact_id")
    computed = _dry_run_artifact_id(identity)
    if artifact_id != computed:
        raise PromotionError(f"retained dry-run artifact ID mismatch: expected {computed}")
    manifest_sha256 = sha256_bytes(manifest_path.read_bytes())
    return RetainedDryRunArtifacts(
        artifact_id=str(artifact_id),
        artifact_hex=match.group(1),
        root=root,
        manifest=manifest_path,
        manifest_sha256=manifest_sha256,
        public_dir=root / "public",
        worker_dir=root / "worker",
        worker_entry=worker_entry,
        config=root / "wrangler.jsonc",
    )


def retain_dry_run_artifacts(
    prepared: PreparedPromotion,
    store_input: Path,
    *,
    repo_root: Path,
    receipt_root: Path,
) -> RetainedDryRunArtifacts:
    if prepared.history_raw is None or set(prepared.history_raw) != {"deployments", "versions"}:
        raise PromotionError("raw Wrangler deployment/version history is unavailable for retention")
    if sha256_bytes(prepared.initial_selector.body) != prepared.initial_selector.body_sha256:
        raise PromotionError("initial selector body differs from its recorded digest")
    if sha256_bytes(prepared.predeploy_status_before) != prepared.predeploy.raw_sha256:
        raise PromotionError("status-before body differs from its recorded deployment digest")
    for key in ("deployments", "versions"):
        body = prepared.history_raw[key]
        if not isinstance(body, bytes):
            raise PromotionError(f"raw Wrangler {key} history must be bytes")
        recorded = prepared.history.get(key)
        if (
            not isinstance(recorded, dict)
            or recorded.get("sha256") != sha256_bytes(body)
            or recorded.get("bytes") != len(body)
        ):
            raise PromotionError(f"raw Wrangler {key} history differs from its evidence")
    protected = [
        repo_root,
        prepared.candidate.root,
        prepared.public_baseline.root,
        receipt_root,
        prepared.public_dir.parent,
    ]
    if prepared.prior is not None:
        protected.append(prepared.prior.root)
    store = _prepare_dry_run_store(store_input, protected)
    pending = store / f".pending-{os.getpid()}-{uuid.uuid4().hex}"
    with _dry_run_store_lock(store), contextlib.ExitStack() as descriptors:
        _assert_external_dry_run_store(store, protected)
        store_fd = os.open(store, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        descriptors.callback(os.close, store_fd)
        store_info = os.fstat(store_fd)
        pending_info: os.stat_result | None = None
        final: Path | None = None

        def verify_store() -> None:
            _verify_store_identity(store, store_fd, store_info)
            _assert_external_dry_run_store(store, protected)

        try:
            verify_store()
            os.mkdir(pending.name, mode=0o700, dir_fd=store_fd)
            pending_info = os.stat(pending.name, dir_fd=store_fd, follow_symlinks=False)
            _copy_sealed_tree(prepared.public_dir, pending / "public", prepared.public_inventory)
            _copy_sealed_tree(prepared.bundle_dir, pending / "worker", prepared.bundle_inventory)
            config_body = prepared.deploy_config.read_bytes()
            if sha256_bytes(config_body) != prepared.deploy_config_sha256:
                raise PromotionError("sealed Wrangler config changed before dry-run retention")
            _write_exclusive(pending / "wrangler.jsonc", config_body)
            raw_evidence = {
                "initial_selector": prepared.initial_selector.body,
                "status_before": prepared.predeploy_status_before,
                "status_after": prepared.predeploy_status_after,
                "deployments_history": prepared.history_raw["deployments"],
                "versions_history": prepared.history_raw["versions"],
            }
            evidence_manifest: dict[str, object] = {}
            for key, relative in _DRY_RUN_EVIDENCE_PATHS.items():
                body = raw_evidence[key]
                _write_exclusive(pending / relative, body)
                evidence_manifest[key] = _file_evidence(relative, body)
            entry_relative = prepared.bundle_entry.relative_to(prepared.bundle_dir).as_posix()
            identity: dict[str, object] = {
                "schema": DRY_RUN_ARTIFACT_SCHEMA,
                "attempt_id": prepared.attempt_id,
                "release_id": prepared.candidate.release_id,
                "authority_git_commit": prepared.candidate.authority_commit,
                "public_tree": _relative_tree_inventory(pending / "public", "public"),
                "worker_bundle": {
                    **_relative_tree_inventory(pending / "worker", "worker"),
                    "entry": f"worker/{entry_relative}",
                },
                "wrangler_config": _file_evidence("wrangler.jsonc", config_body),
                "evidence": evidence_manifest,
            }
            artifact_id = _dry_run_artifact_id(identity)
            artifact_hex = artifact_id.removeprefix("sha256:")
            manifest = {**identity, "artifact_id": artifact_id}
            _write_exclusive(pending / "manifest.json", canonical_json_bytes(manifest))
            _seal_retained_tree(pending)
            _verify_directory_at(store_fd, pending.name, pending_info)
            verify_store()
            final = store / artifact_hex
            try:
                _publish_no_replace(store_fd, pending.name, final.name)
                final_info = pending_info
            except FileExistsError:
                final_info = os.stat(final.name, dir_fd=store_fd, follow_symlinks=False)
            verify_store()
            _verify_directory_at(store_fd, final.name, final_info)
            verified = verify_retained_dry_run_artifacts(
                final, expected_artifact_id=artifact_id
            )
            _verify_directory_at(store_fd, final.name, final_info)
            verify_store()
            _remove_owned_directory_at(store_fd, pending.name, pending_info)
            _verify_directory_at(store_fd, final.name, final_info)
            verify_store()
            os.fsync(store_fd)
            return verified
        except BaseException as original_error:
            if pending_info is not None:
                for candidate in (pending, final):
                    if candidate is None:
                        continue
                    try:
                        _remove_owned_directory_at(store_fd, candidate.name, pending_info)
                    except Exception as cleanup_error:
                        original_error.add_note(f"retained artifact cleanup failed at {candidate.name}: {cleanup_error}")
            if isinstance(original_error, BaselineFreezeError):
                raise PromotionError(str(original_error)) from original_error
            raise


def selector_from_probe(
    probe: SelectorProbe,
    candidate_release_id: str,
    *,
    candidate_manifest_sha256: str | None = None,
    allow_first_deploy: bool,
) -> SelectorState:
    if probe.status == 404:
        if not allow_first_deploy:
            raise PromotionError(
                "production selector is absent; pass --allow-first-deploy-without-selector"
            )
        return SelectorState(404, probe.sha256, probe.body, None, None, None, None)
    if probe.status != 200:
        raise PromotionError(f"production selector returned HTTP {probe.status}")
    if allow_first_deploy:
        raise PromotionError(
            "--allow-first-deploy-without-selector was supplied but production has a selector"
        )
    value = _decode_json(probe.body, "production selector")
    if not isinstance(value, dict):
        raise PromotionError("production selector must be an object")
    schema = value.get("schema")
    if schema == SELECTOR_SCHEMA_V1:
        current_keys = {"release_id", "release", "manifest"}
        previous_keys = {
            "previous_release_id",
            "previous_release",
            "previous_manifest",
        }
    elif schema == SELECTOR_SCHEMA:
        current_keys = {"release_id", "release", "manifest_sha256", "manifest"}
        previous_keys = {
            "previous_release_id",
            "previous_release",
            "previous_manifest_sha256",
            "previous_manifest",
        }
    else:
        raise PromotionError("production selector schema mismatch")
    allowed = {"schema", "audited_at", *current_keys, *previous_keys}
    unknown = sorted(set(value) - allowed)
    if unknown:
        raise PromotionError(f"production selector has unknown fields: {unknown}")
    missing = sorted(current_keys - set(value))
    if missing:
        raise PromotionError(f"production selector is missing fields: {missing}")

    def checked(prefix: str = "") -> tuple[str, str | None]:
        release_id = value.get(prefix + "release_id")
        release_hex = value.get(prefix + "release")
        manifest = value.get(prefix + "manifest")
        match = RELEASE_ID_RE.fullmatch(str(release_id or ""))
        if match is None or release_hex != match.group(1):
            raise PromotionError(f"production selector {prefix or 'current '}release is inconsistent")
        if schema == SELECTOR_SCHEMA_V1:
            expected_manifest = f"/assets/brain/releases/{match.group(1)}/release.json"
            manifest_sha256 = None
        else:
            raw_manifest_sha256 = value.get(prefix + "manifest_sha256")
            if (
                not isinstance(raw_manifest_sha256, str)
                or re.fullmatch(r"[0-9a-f]{64}", raw_manifest_sha256) is None
            ):
                raise PromotionError(
                    f"production selector {prefix or 'current '}manifest digest is invalid"
                )
            manifest_sha256 = raw_manifest_sha256
            expected_manifest = (
                f"/assets/brain/releases/{manifest_sha256}/release.json"
            )
        if manifest != expected_manifest:
            raise PromotionError(f"production selector {prefix or 'current '}release is inconsistent")
        return str(release_id), manifest_sha256

    current, current_manifest_sha256 = checked()
    if (
        schema == SELECTOR_SCHEMA_V1
        and current == candidate_release_id
        and candidate_manifest_sha256 is not None
    ):
        raise PromotionError(
            "legacy production selector cannot distinguish the live manifest from a "
            "same-logical-release candidate"
        )
    present = [key for key in previous_keys if key in value]
    previous: str | None = None
    previous_manifest_sha256: str | None = None
    if present:
        if len(present) != len(previous_keys):
            raise PromotionError("production selector previous release fields are incomplete")
        previous, previous_manifest_sha256 = checked("previous_")
        if schema == SELECTOR_SCHEMA_V1 and previous == current:
            raise PromotionError("production selector current and previous releases are identical")
        if (
            schema == SELECTOR_SCHEMA
            and previous_manifest_sha256 == current_manifest_sha256
        ):
            raise PromotionError("production selector current and previous manifests are identical")
    audited_at = value.get("audited_at")
    if audited_at is not None and (not isinstance(audited_at, str) or not audited_at):
        raise PromotionError("production selector audited_at is invalid")
    candidate_is_current = current == candidate_release_id
    if schema == SELECTOR_SCHEMA:
        if candidate_manifest_sha256 is None:
            raise PromotionError("candidate manifest digest is required for a v2 selector")
        candidate_is_current = current_manifest_sha256 == candidate_manifest_sha256
    retained = previous if candidate_is_current else current
    retained_manifest_sha256 = (
        previous_manifest_sha256 if candidate_is_current else current_manifest_sha256
    )
    return SelectorState(
        200,
        probe.sha256,
        probe.body,
        current,
        previous,
        retained,
        audited_at,
        current_manifest_sha256,
        previous_manifest_sha256,
        retained_manifest_sha256,
    )


def parse_deployment_status(output: bytes) -> DeploymentState:
    value = extract_last_json_value(output, "Wrangler deployment status")
    if not isinstance(value, dict):
        raise PromotionError("Wrangler deployment status must be an object")
    deployment_id = value.get("id")
    versions = value.get("versions")
    if not isinstance(deployment_id, str) or UUID_RE.fullmatch(deployment_id) is None:
        raise PromotionError("Wrangler deployment status has no lowercase deployment UUID")
    if not isinstance(versions, list) or len(versions) != 1 or not isinstance(versions[0], dict):
        raise PromotionError("production traffic is not one unambiguous Worker version")
    version_id = versions[0].get("version_id")
    percentage = versions[0].get("percentage")
    if (
        not isinstance(version_id, str)
        or UUID_RE.fullmatch(version_id) is None
        or not isinstance(percentage, int)
        or isinstance(percentage, bool)
        or percentage != 100
    ):
        raise PromotionError("production traffic is not one lowercase Worker UUID at 100%")
    return DeploymentState(deployment_id, version_id, sha256_bytes(output))


def parse_candidate_version(output: bytes) -> str | None:
    matches = re.findall(
        rb"(?m)^\s*Current Version ID:\s*"
        rb"([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})\s*$",
        output,
    )
    if len(matches) != 1:
        return None
    return matches[0].decode("ascii")


def command_evidence(result: RunResult, journal: EventJournal, prefix: str) -> dict[str, object]:
    stdout = result.stdout[:MAX_COMMAND_EVIDENCE_BYTES]
    stderr = result.stderr[:MAX_COMMAND_EVIDENCE_BYTES]
    stdout_ref = journal.append_blob(f"{prefix}-stdout", stdout, "text/plain; charset=utf-8")
    stderr_ref = journal.append_blob(f"{prefix}-stderr", stderr, "text/plain; charset=utf-8")
    return {
        "args": list(result.args),
        "returncode": result.returncode,
        "timed_out": result.timed_out,
        "stdout": stdout_ref,
        "stderr": stderr_ref,
        "stdout_full_sha256": sha256_bytes(result.stdout),
        "stderr_full_sha256": sha256_bytes(result.stderr),
        "stdout_truncated": len(result.stdout) > len(stdout),
        "stderr_truncated": len(result.stderr) > len(stderr),
    }


class BrainPromoter:
    def __init__(
        self,
        *,
        repo_root: Path,
        python: Path,
        release_id: str | None,
        release_root: Path | None,
        public_baseline_id: str | None,
        public_baseline_root: Path | None,
        activation_bundle_id: str | None = None,
        activation_bundle_root: Path | None = None,
        expected_semantic_baseline_id: str | None = None,
        receipt_root: Path,
        base_url: str,
        mode: str,
        production_origin: str = PRODUCTION_ORIGIN,
        allow_first_deploy: bool = False,
        first_deploy_approval: str | None = None,
        approval_note: str | None = None,
        reconcile_attempt: str | None = None,
        reconcile_quiet_seconds: float = 900,
        confirm_no_production_change: bool = False,
        no_change_approval: str | None = None,
        accept_external_supersession: bool = False,
        external_supersession_approval: str | None = None,
        canary_timeout: float = 300,
        canary_interval: float = 5,
        canary_max_response_bytes: int = 32 * 1024 * 1024,
        status_attempts: int = 12,
        status_interval: float = 5,
        command_timeout: float = 900,
        runner: CommandRunner | None = None,
        sleeper: Callable[[float], None] = time.sleep,
        attempt_id: str | None = None,
        audited_at: str | None = None,
        selector_opener: Callable[..., object] | None = None,
        retain_dry_run_store: Path | None = None,
    ) -> None:
        self.repo = repo_root.absolute()
        self.python = python.absolute()
        self.release_id = release_id
        self.release_root = release_root
        self.public_baseline_id = public_baseline_id
        self.public_baseline_root = public_baseline_root
        self.activation_bundle_id = activation_bundle_id
        self.activation_bundle_root = activation_bundle_root
        self.expected_semantic_baseline_id = expected_semantic_baseline_id
        self.receipt_root_input = receipt_root
        self.base_url = require_https_base_url(base_url)
        self.production_origin = require_https_base_url(production_origin)
        self.mode = mode
        self.allow_first_deploy = allow_first_deploy
        self.first_deploy_approval = first_deploy_approval
        self.approval_note = approval_note
        self.reconcile_attempt = reconcile_attempt
        self.reconcile_quiet_seconds = reconcile_quiet_seconds
        self.confirm_no_production_change = confirm_no_production_change
        self.no_change_approval = no_change_approval
        self.accept_external_supersession = accept_external_supersession
        self.external_supersession_approval = external_supersession_approval
        self.canary_timeout = canary_timeout
        self.canary_interval = canary_interval
        self.canary_max_response_bytes = canary_max_response_bytes
        self.status_attempts = status_attempts
        self.status_interval = status_interval
        self.command_timeout = command_timeout
        self.runner = runner or CommandRunner()
        self.sleep = sleeper
        self.attempt_id = attempt_id or self._new_attempt_id(release_id)
        self.audited_at = audited_at or utc_now()
        self.selector_opener = selector_opener
        self.retain_dry_run_store = retain_dry_run_store
        self._selector_probe_count = 0
        self.wiki = self.repo / "wiki"
        self.receipt_root: Path | None = None
        self._reviewed_node_executables: Mapping[str, object] | None = None

    @staticmethod
    def _new_attempt_id(release_id: str | None) -> str:
        prefix = "reconcile" if release_id is None else release_id.removeprefix("sha256:")[:12]
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        return f"{stamp}-{prefix}-{uuid.uuid4().hex[:10]}"

    def _validate_options(self) -> None:
        def present(value: str | None) -> bool:
            return value is not None and bool(value.strip())

        if not self.repo.is_absolute() or not self.repo.is_dir():
            raise PromotionError("--repo-root must name an existing absolute directory")
        if not self.python.is_file() or not os.access(self.python, os.X_OK):
            raise PromotionError("--python must name an executable interpreter")
        for marker in (
            self.repo / "brain" / "tools" / "verify_release.py",
            self.repo / "wiki" / "scripts" / "build-public.ts",
            self.repo / "wiki" / "wrangler.jsonc",
        ):
            if not marker.is_file():
                raise PromotionError(f"repository marker is missing: {marker}")
        if self.mode not in {"dry-run", "execute", "reconcile"}:
            raise PromotionError(f"unsupported promotion mode: {self.mode}")
        if self.retain_dry_run_store is not None:
            if self.mode != "dry-run":
                raise PromotionError("--retain-dry-run-store is valid only with --dry-run")
            if not self.retain_dry_run_store.is_absolute():
                raise PromotionError("--retain-dry-run-store must be an absolute path")
        if self.base_url != self.production_origin:
            raise PromotionError(
                f"promotion target must be the pinned production origin {self.production_origin}"
            )
        if (
            self.status_attempts <= 0
            or not math.isfinite(self.status_interval)
            or self.status_interval < 0
        ):
            raise PromotionError("status attempts must be positive and interval non-negative")
        if (
            not math.isfinite(self.canary_timeout)
            or not math.isfinite(self.canary_interval)
            or self.canary_timeout < 0
            or self.canary_interval < 0
        ):
            raise PromotionError("canary timeout/interval must be non-negative")
        if (
            not math.isfinite(self.reconcile_quiet_seconds)
            or self.reconcile_quiet_seconds <= 0
        ):
            raise PromotionError("reconciliation quiet interval must be positive")
        if self.canary_max_response_bytes <= 0:
            raise PromotionError("canary response limit must be positive")
        if not math.isfinite(self.command_timeout) or self.command_timeout <= 0:
            raise PromotionError("command timeout must be positive")
        if ATTEMPT_ID_RE.fullmatch(self.attempt_id) is None:
            raise PromotionError("attempt ID is invalid")
        if self.mode == "reconcile":
            if not self.reconcile_attempt or ATTEMPT_ID_RE.fullmatch(self.reconcile_attempt) is None:
                raise PromotionError("--reconcile-attempt requires a valid attempt ID")
            if not present(self.approval_note):
                raise PromotionError("reconciliation requires --approval-note")
            if self.allow_first_deploy or self.first_deploy_approval is not None:
                raise PromotionError("first-deploy options are not valid during reconciliation")
            if any(
                value is not None
                for value in (
                    self.activation_bundle_id,
                    self.activation_bundle_root,
                    self.expected_semantic_baseline_id,
                )
            ):
                raise PromotionError("activation-bundle options are not valid during reconciliation")
            if self.accept_external_supersession and self.confirm_no_production_change:
                raise PromotionError(
                    "choose only one reconciliation resolution: external supersession or no change"
                )
            if self.accept_external_supersession:
                if not present(self.external_supersession_approval):
                    raise PromotionError(
                        "external supersession requires --external-supersession-approval"
                    )
            elif self.external_supersession_approval is not None:
                raise PromotionError(
                    "--external-supersession-approval requires --accept-external-supersession"
                )
            if self.confirm_no_production_change:
                if not present(self.no_change_approval):
                    raise PromotionError(
                        "--confirm-no-production-change requires a non-empty --no-change-approval"
                    )
            elif self.no_change_approval is not None:
                raise PromotionError(
                    "--no-change-approval requires --confirm-no-production-change"
                )
            return
        if any(
            (
                self.confirm_no_production_change,
                self.no_change_approval,
                self.accept_external_supersession,
                self.external_supersession_approval,
            )
        ):
            raise PromotionError("reconciliation resolution options are reconciliation-only")
        if self.release_id is None or RELEASE_ID_RE.fullmatch(self.release_id) is None:
            raise PromotionError("release ID must be sha256:<64 lowercase hex>")
        if self.release_root is None or not self.release_root.is_absolute():
            raise PromotionError("--release-root must be an absolute path")
        if (
            self.public_baseline_id is None
            or RELEASE_ID_RE.fullmatch(self.public_baseline_id) is None
        ):
            raise PromotionError("public baseline ID must be sha256:<64 lowercase hex>")
        if self.public_baseline_root is None or not self.public_baseline_root.is_absolute():
            raise PromotionError("--public-baseline-root must be an absolute path")
        if self.mode == "dry-run":
            if self.approval_note is not None:
                raise PromotionError("--approval-note is valid only with --execute")
            if self.first_deploy_approval is not None:
                raise PromotionError("--first-deploy-approval is valid only with --execute")
        elif self.allow_first_deploy:
            if not present(self.first_deploy_approval):
                raise PromotionError(
                    "--allow-first-deploy-without-selector requires a non-empty "
                    "--first-deploy-approval during execution"
                )
        elif self.first_deploy_approval is not None:
            raise PromotionError(
                "--first-deploy-approval requires --allow-first-deploy-without-selector"
            )
        if self.mode == "execute" and not present(self.approval_note):
            raise PromotionError("execution requires --approval-note")
        activation_values = (
            self.activation_bundle_id,
            self.activation_bundle_root,
            self.expected_semantic_baseline_id,
        )
        if self.mode == "execute":
            if any(value is None for value in activation_values):
                raise PromotionError(
                    "execution requires --activation-bundle-id, --activation-bundle-root, "
                    "and --expected-semantic-baseline-id"
                )
            assert self.activation_bundle_id is not None
            assert self.activation_bundle_root is not None
            assert self.expected_semantic_baseline_id is not None
            if RELEASE_ID_RE.fullmatch(self.activation_bundle_id) is None:
                raise PromotionError("activation bundle ID must be sha256:<64 lowercase hex>")
            if RELEASE_ID_RE.fullmatch(self.expected_semantic_baseline_id) is None:
                raise PromotionError(
                    "expected semantic baseline ID must be sha256:<64 lowercase hex>"
                )
            if not self.activation_bundle_root.is_absolute():
                raise PromotionError("--activation-bundle-root must be an absolute path")
        elif any(value is not None for value in activation_values):
            raise PromotionError("activation-bundle options are valid only with --execute")

    def _run(
        self,
        args: Sequence[str],
        *,
        cwd: Path,
        timeout: float | None = None,
        env: Mapping[str, str] | None = None,
    ) -> RunResult:
        return self.runner.run(
            args,
            cwd=cwd,
            timeout=timeout or self.command_timeout,
            env=env,
        )

    def _require_command(
        self,
        args: Sequence[str],
        *,
        cwd: Path,
        label: str,
        timeout: float | None = None,
        env: Mapping[str, str] | None = None,
    ) -> RunResult:
        result = self._run(args, cwd=cwd, timeout=timeout, env=env)
        if not result.ok:
            detail = (result.stderr or result.stdout).decode("utf-8", errors="replace")[-2000:]
            state = "timed out" if result.timed_out else f"returned {result.returncode}"
            raise PromotionError(f"{label} {state}: {detail.strip()}")
        return result

    def _node_executables_identity(self) -> dict[str, object]:
        wrangler_entry = (
            self.wiki
            / "node_modules"
            / "wrangler"
            / "bin"
            / "wrangler.js"
        )
        if wrangler_entry.is_symlink() or not wrangler_entry.is_file():
            raise PromotionError("installed Wrangler entry point is missing or symlinked")
        wrangler_body = wrangler_entry.read_bytes()
        return {
            "schema": "wikilean.node-executables/v1",
            "node": _executable_identity("node"),
            "wrangler": {
                "path": str(wrangler_entry),
                "sha256": sha256_bytes(wrangler_body),
                "bytes": len(wrangler_body),
            },
        }

    def _bind_node_executables(self, identity: Mapping[str, object]) -> None:
        if identity.get("schema") != "wikilean.node-executables/v1":
            raise PromotionError("reviewed Node executable identity is malformed")
        for name in ("node", "wrangler"):
            path = Path(self._node_tool_path(identity, name))
            if not path.is_absolute():
                raise PromotionError(f"reviewed {name} executable path is not absolute")
        self._reviewed_node_executables = copy.deepcopy(dict(identity))

    def _reviewed_node_tool_path(self, name: str) -> str:
        if self._reviewed_node_executables is None:
            raise PromotionError("Node/Wrangler executables were not reviewed")
        return self._node_tool_path(self._reviewed_node_executables, name)

    def _reviewed_node_environment(
        self, *, include_cloudflare: bool = False
    ) -> dict[str, str]:
        return _node_environment(
            Path(self._reviewed_node_tool_path("node")),
            include_cloudflare=include_cloudflare,
        )

    def _reviewed_node_executables_now(self) -> dict[str, object]:
        if self._reviewed_node_executables is None:
            raise PromotionError("Node/Wrangler executables were not reviewed")
        node_path = Path(self._reviewed_node_tool_path("node"))
        wrangler_path = Path(self._reviewed_node_tool_path("wrangler"))
        try:
            resolved_node = node_path.resolve(strict=True)
        except OSError as exc:
            raise PromotionError(f"reviewed Node executable is unavailable: {exc}") from exc
        if resolved_node != node_path or not node_path.is_file() or not os.access(node_path, os.X_OK):
            raise PromotionError("reviewed Node path is not a canonical executable file")
        if wrangler_path.is_symlink() or not wrangler_path.is_file():
            raise PromotionError("reviewed Wrangler entry point is unavailable or symlinked")
        if wrangler_path != self.wiki / "node_modules" / "wrangler" / "bin" / "wrangler.js":
            raise PromotionError("reviewed Wrangler entry point is outside this checkout")
        node_body = node_path.read_bytes()
        wrangler_body = wrangler_path.read_bytes()
        return {
            "schema": "wikilean.node-executables/v1",
            "node": {
                "path": str(node_path),
                "sha256": sha256_bytes(node_body),
                "bytes": len(node_body),
            },
            "wrangler": {
                "path": str(wrangler_path),
                "sha256": sha256_bytes(wrangler_body),
                "bytes": len(wrangler_body),
            },
        }

    @staticmethod
    def _node_tool_path(identity: Mapping[str, object], name: str) -> str:
        tool = identity.get(name)
        if not isinstance(tool, dict) or not isinstance(tool.get("path"), str):
            raise PromotionError(f"reviewed {name} executable identity is malformed")
        return tool["path"]

    @staticmethod
    def _node_command(name: str, *arguments: str) -> list[str]:
        return [str(_resolved_executable(name)), *arguments]

    def _wrangler_command(self, *arguments: str) -> list[str]:
        return [
            self._reviewed_node_tool_path("node"),
            self._reviewed_node_tool_path("wrangler"),
            *arguments,
        ]

    def _git_text(self, *args: str, allow_failure: bool = False) -> str:
        environment = dict(os.environ)
        for name in tuple(environment):
            if name in {
                "GIT_DIR",
                "GIT_WORK_TREE",
                "GIT_INDEX_FILE",
                "GIT_OBJECT_DIRECTORY",
                "GIT_ALTERNATE_OBJECT_DIRECTORIES",
                "GIT_COMMON_DIR",
                "GIT_NAMESPACE",
                "GIT_REPLACE_REF_BASE",
            } or name.startswith("GIT_CONFIG_"):
                environment.pop(name, None)
        environment["GIT_NO_REPLACE_OBJECTS"] = "1"
        environment["GIT_OPTIONAL_LOCKS"] = "0"
        environment["LC_ALL"] = "C"
        result = self._run(
            ["git", "-C", str(self.repo), *args],
            cwd=self.repo,
            timeout=60,
            env=environment,
        )
        if not result.ok:
            if allow_failure:
                return ""
            raise PromotionError(
                f"git {' '.join(args)} failed: "
                + result.stderr.decode("utf-8", errors="replace").strip()
            )
        return result.stdout.decode("utf-8", errors="strict").strip()

    def _clean_checkout_state(self) -> tuple[str, str, str]:
        top = Path(self._git_text("rev-parse", "--show-toplevel")).resolve(strict=True)
        if top != self.repo.resolve(strict=True):
            raise PromotionError(f"promotion checkout root mismatch: {top}")
        head = self._git_text("rev-parse", "HEAD")
        main = self._git_text("rev-parse", "refs/heads/main")
        branch = self._git_text("symbolic-ref", "--quiet", "--short", "HEAD", allow_failure=True)
        dirty = self._git_text("status", "--porcelain=v1", "--untracked-files=all")
        if dirty:
            raise PromotionError(f"promotion checkout is dirty:\n{dirty}")
        git_dir = Path(self._git_text("rev-parse", "--absolute-git-dir"))
        if any(
            path.exists()
            for path in (git_dir / "MERGE_HEAD", git_dir / "rebase-merge", git_dir / "rebase-apply")
        ):
            raise PromotionError("promotion checkout has a merge or rebase in progress")
        return head, main, branch

    def _check_git_authority(self, expected_commit: str) -> str:
        head, main, branch = self._clean_checkout_state()
        if head != expected_commit or main != expected_commit:
            raise PromotionError(
                f"release authority {expected_commit} must equal HEAD and refs/heads/main "
                f"(HEAD={head}, main={main})"
            )
        if branch not in {"", "main"}:
            raise PromotionError(f"promotion checkout is on {branch!r}, not main or detached main")
        return head

    def _check_recovery_checkout(self, authority_commit: str) -> str:
        head, main, branch = self._clean_checkout_state()
        current_reviewed_main = head == main and branch in {"", "main"}
        detached_authority = head == authority_commit and branch == ""
        if not current_reviewed_main and not detached_authority:
            raise PromotionError(
                "reconciliation requires either clean current main or a clean detached "
                f"checkout of release authority {authority_commit} "
                f"(HEAD={head}, main={main}, branch={branch or 'detached'})"
            )
        return head

    def _verify_toolchain(self) -> tuple[str, str]:
        if self._reviewed_node_executables is None:
            self._bind_node_executables(self._node_executables_identity())
        tools = self._reviewed_node_executables
        assert tools is not None
        if self._reviewed_node_executables_now() != tools:
            raise PromotionError("Node/Wrangler executable bytes changed before use")
        node_result = self._require_command(
            [self._node_tool_path(tools, "node"), "--version"],
            cwd=self.wiki,
            label="Node version",
            timeout=30,
            env=self._reviewed_node_environment(),
        )
        node_version = node_result.stdout.decode("utf-8", errors="strict").strip()
        if re.fullmatch(r"v22\.[0-9]+\.[0-9]+", node_version) is None:
            raise PromotionError(f"promotion requires Node 22, got {node_version!r}")

        lock_path = self.wiki / "package-lock.json"
        installed_path = self.wiki / "node_modules" / "wrangler" / "package.json"
        if installed_path.is_symlink() or not installed_path.is_file():
            raise PromotionError("installed Wrangler package is missing or symlinked; run npm ci")
        lock = _decode_json(lock_path.read_bytes(), "package-lock.json")
        installed = _decode_json(installed_path.read_bytes(), "installed Wrangler package")
        if not isinstance(lock, dict) or not isinstance(installed, dict):
            raise PromotionError("Node package metadata must be JSON objects")
        packages = lock.get("packages")
        locked = packages.get("node_modules/wrangler") if isinstance(packages, dict) else None
        locked_version = locked.get("version") if isinstance(locked, dict) else None
        installed_version = installed.get("version")
        if (
            not isinstance(locked_version, str)
            or not isinstance(installed_version, str)
            or installed_version != locked_version
        ):
            raise PromotionError(
                "installed Wrangler version does not match package-lock.json; run npm ci"
            )
        wrangler_result = self._require_command(
            [
                self._node_tool_path(tools, "node"),
                self._node_tool_path(tools, "wrangler"),
                "--version",
            ],
            cwd=self.wiki,
            label="Wrangler version",
            timeout=60,
            env=self._reviewed_node_environment(),
        )
        wrangler_output = wrangler_result.stdout.decode("utf-8", errors="replace").strip()
        versions = re.findall(r"(?m)^([0-9]+\.[0-9]+\.[0-9]+)\s*$", wrangler_output)
        if versions != [locked_version]:
            raise PromotionError(
                f"Wrangler CLI version did not equal locked {locked_version}: {wrangler_output!r}"
            )
        return node_version, locked_version

    def _wrangler_installation_identity(self) -> dict[str, object]:
        return inventory_node_modules(self.wiki)

    def _validate_release_root(
        self,
        root_input: Path,
        expected_release_id: str,
        *,
        allow_legacy_release_id_root: bool = False,
    ) -> Path:
        if not root_input.is_absolute():
            raise PromotionError("release root must be absolute")
        if root_input.is_symlink():
            raise PromotionError("release root must not be a symlink")
        try:
            root = root_input.resolve(strict=True)
        except OSError as exc:
            raise PromotionError(f"release root is unavailable: {exc}") from exc
        if root != root_input.absolute():
            raise PromotionError("release root must not traverse symlink aliases")
        if not root.is_dir() or _is_relative_to(root, self.repo.resolve(strict=True)):
            raise PromotionError("release root must be a directory outside the promotion checkout")
        match = RELEASE_ID_RE.fullmatch(expected_release_id)
        assert match is not None
        manifest = root / "release.json"
        if manifest.is_symlink() or not manifest.is_file():
            raise PromotionError("release root must contain a regular release.json")
        manifest_bytes = manifest.read_bytes()
        manifest_value = _decode_json(manifest_bytes, "release manifest")
        if (
            not isinstance(manifest_value, dict)
            or manifest_value.get("release_id") != expected_release_id
        ):
            raise PromotionError("release root manifest does not match the requested release ID")
        manifest_sha256 = sha256_bytes(manifest_bytes)
        allowed_names = {manifest_sha256}
        if allow_legacy_release_id_root:
            allowed_names.add(match.group(1))
        if root.name not in allowed_names:
            expected = "manifest digest"
            if allow_legacy_release_id_root:
                expected += " (or legacy release ID)"
            raise PromotionError(f"release root basename does not match the {expected}")
        return root

    def _verify_release(
        self,
        expected_release_id: str,
        root_input: Path,
        *,
        allow_legacy_release_id_root: bool = False,
    ) -> ReleaseInfo:
        root = self._validate_release_root(
            root_input,
            expected_release_id,
            allow_legacy_release_id_root=allow_legacy_release_id_root,
        )
        manifest = root / "release.json"
        result = self._require_command(
            [
                str(self.python),
                str(self.repo / "brain" / "tools" / "verify_release.py"),
                "--manifest",
                str(manifest),
                "--root",
                str(root),
            ],
            cwd=self.repo,
            label="release verification",
            timeout=self.command_timeout,
        )
        verified = extract_last_json_value(result.stdout, "release verification")
        if (
            not isinstance(verified, dict)
            or verified.get("ok") is not True
            or verified.get("release_id") != expected_release_id
        ):
            raise PromotionError("release verifier did not confirm the requested release ID")
        manifest_bytes = manifest.read_bytes()
        value = _decode_json(manifest_bytes, "release manifest")
        if not isinstance(value, dict) or value.get("schema") != RELEASE_SCHEMA:
            raise PromotionError("release manifest schema mismatch")
        if value.get("release_id") != expected_release_id:
            raise PromotionError("release manifest ID differs from the requested release")
        authority = value.get("authority")
        reducer = value.get("reducer")
        if not isinstance(authority, dict) or not isinstance(reducer, dict):
            raise PromotionError("release manifest omitted authority/reducer identity")
        authority_commit = authority.get("git_commit")
        reducer_commit = reducer.get("git_commit")
        if (
            not isinstance(authority_commit, str)
            or GIT_COMMIT_RE.fullmatch(authority_commit) is None
            or not isinstance(reducer_commit, str)
            or GIT_COMMIT_RE.fullmatch(reducer_commit) is None
            or reducer_commit != authority_commit
        ):
            raise PromotionError("release authority and reducer commits are invalid or disagree")
        return ReleaseInfo(
            expected_release_id,
            expected_release_id.removeprefix("sha256:"),
            root,
            manifest,
            sha256_bytes(manifest_bytes),
            authority_commit,
            reducer_commit,
            inventory_tree(root),
        )

    def _probe_selector(self, phase: str) -> tuple[SelectorProbe, str]:
        self._selector_probe_count += 1
        try:
            probe = probe_selector(
                self.base_url,
                nonce=f"{self.attempt_id}-{phase}-{self._selector_probe_count}",
                opener=self.selector_opener,
            )
        except TransportError as exc:
            raise PromotionError(f"selector transport preflight failed: {exc}") from exc
        trust_source = getattr(probe, "trust_source", "injected" if self.selector_opener else "unknown")
        return probe, str(trust_source)

    def _wrangler_status(
        self, config: Path | None = None
    ) -> tuple[DeploymentState, RunResult]:
        command = self._wrangler_command("deployments", "status")
        if config is not None:
            command.extend(["--config", str(config)])
        command.append("--json")
        result = self._require_command(
            command,
            cwd=self.wiki,
            label="Wrangler deployment status",
            timeout=120,
            env=self._reviewed_node_environment(include_cloudflare=True),
        )
        return parse_deployment_status(result.stdout), result

    def _wrangler_version_annotations(
        self, version_id: str, config: Path | None = None
    ) -> tuple[dict[str, str], RunResult]:
        if UUID_RE.fullmatch(version_id) is None:
            raise PromotionError("refusing to inspect a malformed Worker version ID")
        command = self._wrangler_command("versions", "view", version_id)
        if config is not None:
            command.extend(["--config", str(config)])
        command.append("--json")
        result = self._require_command(
            command,
            cwd=self.wiki,
            label="Wrangler version view",
            timeout=120,
            env=self._reviewed_node_environment(include_cloudflare=True),
        )
        value = extract_last_json_value(result.stdout, "Wrangler version view")
        if not isinstance(value, dict) or value.get("id") != version_id:
            raise PromotionError("Wrangler version view does not name the requested version")
        annotations = value.get("annotations")
        if not isinstance(annotations, dict):
            raise PromotionError("Wrangler version view omitted annotations")
        return {
            key: item
            for key, item in annotations.items()
            if isinstance(key, str) and isinstance(item, str)
        }, result

    def _history_evidence(
        self, config: Path | None = None
    ) -> tuple[dict[str, object], dict[str, bytes]]:
        evidence: dict[str, object] = {}
        raw: dict[str, bytes] = {}
        for key, parts in (
            ("deployments", ["deployments", "list"]),
            ("versions", ["versions", "list"]),
        ):
            command = self._wrangler_command(*parts)
            if config is not None:
                command.extend(["--config", str(config)])
            command.append("--json")
            result = self._require_command(
                command,
                cwd=self.wiki,
                label=f"Wrangler {key} history",
                timeout=120,
                env=self._reviewed_node_environment(include_cloudflare=True),
            )
            value = extract_last_json_value(result.stdout, f"Wrangler {key} history")
            if not isinstance(value, (dict, list)):
                raise PromotionError(f"Wrangler {key} history returned the wrong JSON type")
            evidence[key] = {
                "sha256": sha256_bytes(result.stdout),
                "bytes": len(result.stdout),
                "entries": len(value) if isinstance(value, list) else None,
            }
            raw[key] = result.stdout
        return evidence, raw

    def _attempt_history(
        self, tag: str, message: str, config: Path | None = None
    ) -> dict[str, object]:
        versions_command = self._wrangler_command("versions", "list")
        deployments_command = self._wrangler_command("deployments", "list")
        if config is not None:
            versions_command.extend(["--config", str(config)])
            deployments_command.extend(["--config", str(config)])
        versions_command.append("--json")
        deployments_command.append("--json")
        versions_result = self._require_command(
            versions_command,
            cwd=self.wiki,
            label="Wrangler version history",
            timeout=120,
            env=self._reviewed_node_environment(include_cloudflare=True),
        )
        deployments_result = self._require_command(
            deployments_command,
            cwd=self.wiki,
            label="Wrangler deployment history",
            timeout=120,
            env=self._reviewed_node_environment(include_cloudflare=True),
        )
        versions_value = extract_last_json_value(
            versions_result.stdout, "Wrangler version history"
        )
        deployments_value = extract_last_json_value(
            deployments_result.stdout, "Wrangler deployment history"
        )
        if not isinstance(versions_value, list) or not isinstance(deployments_value, list):
            raise PromotionError("Wrangler history did not return arrays")
        matched_versions: list[str] = []
        for item in versions_value:
            if not isinstance(item, dict):
                continue
            version_id = item.get("id")
            annotations = item.get("annotations")
            if (
                isinstance(version_id, str)
                and UUID_RE.fullmatch(version_id) is not None
                and isinstance(annotations, dict)
                and annotations.get("workers/tag") == tag
                and annotations.get("workers/message") == message
            ):
                matched_versions.append(version_id)
        version_set = set(matched_versions)
        matched_deployments: list[dict[str, object]] = []
        for item in deployments_value:
            if not isinstance(item, dict):
                continue
            deployment_id = item.get("id")
            versions = item.get("versions")
            if (
                not isinstance(deployment_id, str)
                or UUID_RE.fullmatch(deployment_id) is None
                or not isinstance(versions, list)
            ):
                continue
            referenced = sorted(
                {
                    version.get("version_id")
                    for version in versions
                    if isinstance(version, dict)
                    and isinstance(version.get("version_id"), str)
                    and version.get("version_id") in version_set
                }
            )
            if referenced:
                matched_deployments.append(
                    {"deployment_id": deployment_id, "version_ids": referenced}
                )
        return {
            "version_ids": sorted(version_set),
            "deployments": matched_deployments,
            "versions_returned": len(versions_value),
            "deployments_returned": len(deployments_value),
            "versions_sha256": sha256_bytes(versions_result.stdout),
            "deployments_sha256": sha256_bytes(deployments_result.stdout),
        }

    def _local_attempt_processes(self, tag: str, message: str) -> list[dict[str, object]]:
        result = self._require_command(
            ["ps", "-axo", "pid=,command="],
            cwd=self.repo,
            label="local process inspection",
            timeout=30,
        )
        matches: list[dict[str, object]] = []
        for raw_line in result.stdout.decode("utf-8", errors="replace").splitlines():
            stripped = raw_line.strip()
            if not stripped:
                continue
            pid_text, separator, command = stripped.partition(" ")
            if (
                separator
                and pid_text.isdigit()
                and int(pid_text) != os.getpid()
                and "wrangler" in command
                and tag in command
                and message in command
            ):
                matches.append(
                    {
                        "pid": int(pid_text),
                        "command_sha256": sha256_bytes(command.encode("utf-8")),
                    }
                )
        return matches

    def _quiet_fence_uncertain_invocation(
        self,
        *,
        journal: EventJournal,
        first: ReconciliationObservation,
        tag: str,
        message: str,
        config: Path,
        recorded_command_timeout: float,
        deploy_invocation_started: bool,
        phase_prefix: str,
        approval: str | None,
        forbid_attempt_deployments: bool,
    ) -> tuple[ReconciliationObservation, dict[str, object] | None]:
        history_before: dict[str, object] | None = None
        history_after: dict[str, object] | None = None
        if deploy_invocation_started:
            if self.reconcile_quiet_seconds < recorded_command_timeout:
                raise PromotionError(
                    "reconciliation quiet interval must be at least the recorded "
                    f"command timeout ({recorded_command_timeout:g}s) after a deployment invocation"
                )
            processes_before = self._local_attempt_processes(tag, message)
            if processes_before:
                raise PromotionError(
                    "a local Wrangler process for this attempt is still running"
                )
            history_before = self._attempt_history(tag, message, config)
            append_event(
                journal,
                "observation",
                {
                    "phase": f"{phase_prefix}_history_before_quiet",
                    "history": history_before,
                    "local_processes": processes_before,
                    "approval": approval,
                },
            )

        self.sleep(self.reconcile_quiet_seconds)
        quiet = self._stable_reconciliation_observation(config)
        self._record_reconciliation_observation(
            journal, quiet, phase=f"{phase_prefix}_quiet_fence"
        )
        if not self._same_reconciliation_state(first, quiet):
            raise PromotionError(
                "live state changed during the reconciliation quiet interval"
            )

        if deploy_invocation_started:
            processes_after = self._local_attempt_processes(tag, message)
            if processes_after:
                raise PromotionError(
                    "a local Wrangler process for this attempt survived the quiet interval"
                )
            history_after = self._attempt_history(tag, message, config)
            append_event(
                journal,
                "observation",
                {
                    "phase": f"{phase_prefix}_history_after_quiet",
                    "history": history_after,
                    "local_processes": processes_after,
                    "approval": approval,
                },
            )
            if history_before is None:
                raise PromotionError("missing pre-quiet deployment history evidence")
            if (
                history_before.get("version_ids") != history_after.get("version_ids")
                or history_before.get("deployments")
                != history_after.get("deployments")
            ):
                raise PromotionError(
                    "attempt-correlated Wrangler history changed during the quiet interval"
                )
            if forbid_attempt_deployments and history_after.get("deployments"):
                raise PromotionError(
                    "Wrangler history contains an attempt-correlated deployment"
                )
        return quiet, history_after

    def _stage_public(
        self,
        candidate: ReleaseInfo,
        prior: ReleaseInfo | None,
        baseline: PublicAssetBaseline,
        public_dir: Path,
    ) -> tuple[dict[str, object], SelectorState]:
        args = [
            self._reviewed_node_tool_path("node"),
            "--experimental-strip-types",
            "scripts/build-public.ts",
            "--public-dir",
            str(public_dir),
            "--public-baseline-manifest",
            str(baseline.manifest_path),
            "--public-baseline-dir",
            str(baseline.root),
            "--brain-audited-at",
            self.audited_at,
            "--brain-release-manifest",
            str(candidate.manifest),
            "--brain-release-dir",
            str(candidate.root),
        ]
        previous = prior or candidate
        args.extend(
            [
                "--brain-previous-release-manifest",
                str(previous.manifest),
                "--brain-previous-release-dir",
                str(previous.root),
            ]
        )
        result = self._require_command(
            args,
            cwd=self.wiki,
            label="external public staging",
            env=self._reviewed_node_environment(),
        )
        value = extract_last_json_value(result.stdout, "external public staging")
        if not isinstance(value, dict) or value.get("schema") != "wikilean.public-build-result/v1":
            raise PromotionError("public staging result schema mismatch")
        if Path(str(value.get("public_dir", ""))).resolve(strict=True) != public_dir.resolve(strict=True):
            raise PromotionError("public staging wrote a different destination")
        baseline_result = value.get("public_baseline")
        if (
            not isinstance(baseline_result, dict)
            or baseline_result.get("schema") != "wikilean.public-asset-baseline/v1"
            or baseline_result.get("baseline_id") != baseline.baseline_id
            or baseline_result.get("authority_commit") != baseline.authority_git_commit
            or Path(str(baseline_result.get("root", ""))).resolve(strict=True)
            != baseline.root
            or baseline_result.get("files") != len(baseline.files)
            or baseline_result.get("bytes") != baseline.total_bytes
        ):
            raise PromotionError("public staging did not preserve the verified asset baseline")
        brain = value.get("brain")
        expected_previous = prior.release_id if prior is not None else None
        expected_previous_manifest = prior.manifest_sha256 if prior is not None else None
        expected_retained = [candidate.release_id, *([expected_previous] if expected_previous else [])]
        expected_retained_manifests = [
            candidate.manifest_sha256,
            *([expected_previous_manifest] if expected_previous_manifest else []),
        ]
        if (
            not isinstance(brain, dict)
            or brain.get("schema") != "wikilean.public-stage-result/v2"
            or brain.get("release_id") != candidate.release_id
            or brain.get("manifest_sha256") != candidate.manifest_sha256
            or brain.get("previous_release_id") != expected_previous
            or brain.get("previous_manifest_sha256") != expected_previous_manifest
            or brain.get("retained_release_ids") != expected_retained
            or brain.get("retained_manifest_sha256s") != expected_retained_manifests
            or brain.get("warnings") != []
        ):
            raise PromotionError(
                "public staging did not activate the exact requested current/previous pair"
            )
        staged_page = public_dir / "brain.html"
        if not staged_page.is_file() or staged_page.read_bytes() != (candidate.root / "site/out/brain.html").read_bytes():
            raise PromotionError("staged Brain page differs from the frozen release")
        selector_path = public_dir / "assets" / "brain" / "current.json"
        if selector_path.is_symlink() or not selector_path.is_file():
            raise PromotionError("staged public tree omitted a regular Brain selector")
        selector_body = selector_path.read_bytes()
        staged_selector = selector_from_probe(
            SelectorProbe(
                body=selector_body,
                body_sha256=sha256_bytes(selector_body),
                content_type="application/json",
                status=200,
                trust_source="local-staged-file",
                url=selector_path.as_uri(),
            ),
            candidate.release_id,
            candidate_manifest_sha256=candidate.manifest_sha256,
            allow_first_deploy=False,
        )
        if (
            staged_selector.current_release_id != candidate.release_id
            or staged_selector.current_manifest_sha256 != candidate.manifest_sha256
            or staged_selector.previous_release_id != expected_previous
            or staged_selector.previous_manifest_sha256 != expected_previous_manifest
            or staged_selector.audited_at != self.audited_at
        ):
            raise PromotionError("staged selector identity/retention/audit fields are inconsistent")
        return value, staged_selector

    def _run_worker_checks(
        self,
        public_dir: Path,
        bundle_dir: Path,
        deploy_config: Path,
    ) -> tuple[Path, dict[str, object]]:
        self._require_command(
            self._node_command("npm", "run", "typecheck"),
            cwd=self.wiki,
            label="Worker typecheck",
            env=self._reviewed_node_environment(),
        )
        self._require_command(
            self._node_command("npm", "run", "test:unit"),
            cwd=self.wiki,
            label="Worker unit tests",
            env=self._reviewed_node_environment(),
        )
        self._require_command(
            self._wrangler_command(
                "deploy",
                str(self.wiki / "src" / "index.ts"),
                "--dry-run",
                "--config",
                str(deploy_config),
                "--strict",
                "--assets",
                str(public_dir),
                "--outdir",
                str(bundle_dir),
            ),
            cwd=self.wiki,
            label="Wrangler local deployment dry-run",
            env=self._reviewed_node_environment(),
        )
        bundle_entry = bundle_dir / "index.js"
        if bundle_entry.is_symlink() or not bundle_entry.is_file():
            raise PromotionError("Wrangler dry-run did not emit a regular bundle/index.js")
        initial_inventory = inventory_tree(bundle_dir)

        preview_dir = bundle_dir.parent / "upload-preview"
        self._require_command(
            self._wrangler_command(
                "deploy",
                str(bundle_entry),
                "--config",
                str(deploy_config),
                "--no-bundle",
                "--dry-run",
                "--strict",
                "--assets",
                str(public_dir),
                "--outdir",
                str(preview_dir),
            ),
            cwd=self.wiki,
            label="sealed-bundle upload dry-run",
            env=self._reviewed_node_environment(),
        )
        preview_entry = preview_dir / "index.js"
        if not preview_entry.is_file() or preview_entry.read_bytes() != bundle_entry.read_bytes():
            raise PromotionError("Wrangler no-bundle preview changed the reviewed Worker bundle")
        if inventory_tree(bundle_dir) != initial_inventory:
            raise PromotionError("reviewed Worker bundle changed during upload preview")
        return bundle_entry, initial_inventory

    def _verify_reviewed_activation(
        self,
        expected: ReviewedActivation | None = None,
    ) -> ReviewedActivation:
        assert self.activation_bundle_id is not None
        assert self.activation_bundle_root is not None
        assert self.expected_semantic_baseline_id is not None
        try:
            import brain_activation_bundle as activation_bundle

            verified = activation_bundle.verify_activation_bundle(
                self.activation_bundle_root
            )
        except (OSError, RuntimeError, ValueError) as exc:
            raise PromotionError(f"activation bundle verification failed: {exc}") from exc
        if verified.root != self.activation_bundle_root:
            raise PromotionError("activation bundle verifier returned a different root")
        if verified.bundle_id != self.activation_bundle_id:
            raise PromotionError(
                "activation bundle identity differs from --activation-bundle-id"
            )
        if verified.release_id != self.release_id:
            raise PromotionError("activation bundle names a different candidate release")
        if verified.baseline_id != self.public_baseline_id:
            raise PromotionError("activation bundle names a different public baseline")
        if (
            verified.semantic_baseline_release_id
            != self.expected_semantic_baseline_id
        ):
            raise PromotionError(
                "activation bundle names a different semantic baseline release"
            )

        evidence = next(
            (item for item in verified.files if item.kind == "promoter_dry_run"),
            None,
        )
        if evidence is None:
            raise PromotionError("activation bundle omitted promoter dry-run evidence")
        dry_run_path = verified.root / evidence.path
        dry_run_raw = dry_run_path.read_bytes()
        if (
            len(dry_run_raw) != evidence.bytes
            or sha256_bytes(dry_run_raw) != evidence.sha256
        ):
            raise PromotionError("activation promoter dry-run evidence changed after verification")
        dry_run = _decode_json(dry_run_raw, "activation promoter dry-run")
        if not isinstance(dry_run, dict):
            raise PromotionError("activation promoter dry-run must be an object")
        attempt_id = dry_run.get("attempt_id")
        intent = dry_run.get("proposed_intent")
        if (
            not isinstance(attempt_id, str)
            or ATTEMPT_ID_RE.fullmatch(attempt_id) is None
            or not isinstance(intent, dict)
        ):
            raise PromotionError("activation promoter dry-run identity is malformed")
        retained_reference = intent.get("retained_artifacts")
        if not isinstance(retained_reference, dict):
            raise PromotionError("activation bundle omitted retained dry-run artifacts")
        retained_root = retained_reference.get("root")
        retained_id = retained_reference.get("artifact_id")
        if not isinstance(retained_root, str) or not isinstance(retained_id, str):
            raise PromotionError("activation retained artifact reference is malformed")
        try:
            retained = verify_retained_dry_run_artifacts(
                Path(retained_root), expected_artifact_id=retained_id
            )
        except (OSError, PromotionError) as exc:
            raise PromotionError(
                f"activation retained dry-run artifacts failed verification: {exc}"
            ) from exc
        if (
            retained.manifest_sha256 != retained_reference.get("manifest_sha256")
            or str(retained.manifest) != retained_reference.get("manifest")
        ):
            raise PromotionError("activation retained artifact reference is inconsistent")

        reviewed = ReviewedActivation(
            root=verified.root,
            manifest=verified.manifest_path,
            manifest_sha256=sha256_bytes(verified.manifest_path.read_bytes()),
            bundle_id=verified.bundle_id,
            semantic_baseline_release_id=verified.semantic_baseline_release_id,
            release_id=verified.release_id,
            baseline_id=verified.baseline_id,
            authority_commit=verified.authority_git_commit,
            reducer_commit=verified.reducer_git_commit,
            dry_run_attempt_id=attempt_id,
            intent=copy.deepcopy(intent),
            retained=retained,
        )
        if expected is not None and reviewed.reference() != expected.reference():
            raise PromotionError("activation bundle identity changed before deployment")
        return reviewed

    @staticmethod
    def _reviewed_evidence_body(
        retained: RetainedDryRunArtifacts,
        name: str,
    ) -> bytes:
        relative = _DRY_RUN_EVIDENCE_PATHS[name]
        path = retained.root / relative
        if path.is_symlink() or not path.is_file():
            raise PromotionError(f"retained dry-run evidence is unavailable: {name}")
        return path.read_bytes()

    def _prepare_reviewed_activation(
        self,
        activation: ReviewedActivation,
    ) -> PreparedPromotion:
        assert self.release_id is not None
        assert self.release_root is not None
        assert self.public_baseline_id is not None
        assert self.public_baseline_root is not None
        intent = activation.intent
        audited_at = intent.get("audited_at")
        if not isinstance(audited_at, str) or not audited_at:
            raise PromotionError("activation dry-run audited_at is malformed")
        self.attempt_id = activation.dry_run_attempt_id
        self.audited_at = audited_at

        candidate = self._verify_release(self.release_id, self.release_root)
        if (
            candidate.release_id != activation.release_id
            or candidate.authority_commit != activation.authority_commit
            or candidate.reducer_commit != activation.reducer_commit
            or not self._release_matches_intent(
                candidate,
                manifest_sha256=intent.get("release_manifest_sha256"),
                tree=intent.get("release_tree"),
                authority_commit=intent.get("authority_commit"),
                reducer_commit=intent.get("reducer_commit"),
            )
            or str(candidate.root) != intent.get("release_root")
        ):
            raise PromotionError("candidate release differs from the reviewed activation")
        try:
            public_baseline = verify_public_baseline(
                self.public_baseline_root,
                self.repo.resolve(strict=True),
                expected_baseline_id=self.public_baseline_id,
                expected_authority_git_commit=candidate.authority_commit,
            )
        except BaselineValidationError as exc:
            raise PromotionError(f"public asset baseline verification failed: {exc}") from exc
        reviewed_baseline = intent.get("public_baseline")
        expected_baseline = {
            "baseline_id": public_baseline.baseline_id,
            "root": str(public_baseline.root),
            "manifest": str(public_baseline.manifest_path),
            "manifest_sha256": sha256_bytes(public_baseline.manifest_path.read_bytes()),
            "authority_commit": public_baseline.authority_git_commit,
            "files": len(public_baseline.files),
            "bytes": public_baseline.total_bytes,
        }
        if reviewed_baseline != expected_baseline:
            raise PromotionError("public baseline differs from the reviewed activation")

        prior: ReleaseInfo | None = None
        retained_release = intent.get("retained_release")
        if retained_release is not None:
            if not isinstance(retained_release, dict):
                raise PromotionError("activation retained release is malformed")
            retained_id = retained_release.get("release_id")
            retained_root = retained_release.get("release_root")
            if not isinstance(retained_id, str) or not isinstance(retained_root, str):
                raise PromotionError("activation retained release identity is malformed")
            prior = self._verify_release(
                retained_id,
                Path(retained_root),
                allow_legacy_release_id_root=True,
            )
            if not self._release_matches_intent(
                prior,
                manifest_sha256=retained_release.get("release_manifest_sha256"),
                tree=retained_release.get("release_tree"),
                authority_commit=retained_release.get("authority_commit"),
                reducer_commit=retained_release.get("reducer_commit"),
            ):
                raise PromotionError("retained release differs from the reviewed activation")

        reviewed_worker = intent.get("worker_bundle")
        if not isinstance(reviewed_worker, dict):
            raise PromotionError("activation Worker bundle evidence is malformed")
        self._check_git_authority(candidate.authority_commit)
        node_executables = self._node_executables_identity()
        if reviewed_worker.get("executables") != node_executables:
            raise PromotionError(
                "Node/Wrangler executables differ from the reviewed activation"
            )
        wrangler_installation = self._wrangler_installation_identity()
        if reviewed_worker.get("installation") != wrangler_installation:
            raise PromotionError(
                "installed Wrangler dependency tree differs from the reviewed activation"
            )
        self._bind_node_executables(node_executables)
        node_version, wrangler_version = self._verify_toolchain()
        if (
            reviewed_worker.get("node_version") != node_version
            or reviewed_worker.get("wrangler_version") != wrangler_version
        ):
            raise PromotionError("toolchain differs from the reviewed activation")

        retained = verify_retained_dry_run_artifacts(
            activation.retained.root,
            expected_artifact_id=activation.retained.artifact_id,
        )
        selector_body = self._reviewed_evidence_body(retained, "initial_selector")
        status_before_body = self._reviewed_evidence_body(retained, "status_before")
        status_after_body = self._reviewed_evidence_body(retained, "status_after")
        predeploy = intent.get("predeploy")
        if not isinstance(predeploy, dict):
            raise PromotionError("activation predeploy evidence is malformed")
        selector_status = predeploy.get("selector_status")
        if selector_status not in {200, 404}:
            raise PromotionError("activation selector status is malformed")
        initial_probe = SelectorProbe(
            body=selector_body,
            body_sha256=sha256_bytes(selector_body),
            content_type=(
                "application/json"
                if selector_status == 200
                else "application/octet-stream"
            ),
            status=selector_status,
            trust_source=str(intent.get("trust_source")),
            url=f"{self.base_url}/assets/brain/current.json?activation={activation.bundle_id}",
        )
        initial_selector = selector_from_probe(
            initial_probe,
            candidate.release_id,
            candidate_manifest_sha256=candidate.manifest_sha256,
            allow_first_deploy=self.allow_first_deploy,
        )
        deployment_before = parse_deployment_status(status_before_body)
        deployment_after = parse_deployment_status(status_after_body)
        if not self._same_deployment(deployment_before, deployment_after):
            raise PromotionError("reviewed activation status sandwich is inconsistent")
        expected_predeploy = {
            "deployment_id": deployment_before.deployment_id,
            "version_id": deployment_before.version_id,
            "status_sha256": deployment_before.raw_sha256,
            "selector_status": initial_selector.status,
            "selector_sha256": initial_selector.body_sha256,
            "release_id": initial_selector.current_release_id,
            "manifest_sha256": initial_selector.current_manifest_sha256,
            "previous_release_id": initial_selector.previous_release_id,
            "previous_manifest_sha256": initial_selector.previous_manifest_sha256,
            "audited_at": initial_selector.audited_at,
        }
        if predeploy != expected_predeploy:
            raise PromotionError("predeploy state differs from the reviewed activation")

        staged_path = retained.public_dir / "assets" / "brain" / "current.json"
        staged_body = staged_path.read_bytes()
        staged_selector = selector_from_probe(
            SelectorProbe(
                body=staged_body,
                body_sha256=sha256_bytes(staged_body),
                content_type="application/json",
                status=200,
                trust_source="reviewed-retained-artifact",
                url=staged_path.as_uri(),
            ),
            candidate.release_id,
            candidate_manifest_sha256=candidate.manifest_sha256,
            allow_first_deploy=False,
        )
        reviewed_staged = intent.get("staged_selector")
        expected_staged = {
            "sha256": staged_selector.body_sha256,
            "release_id": staged_selector.current_release_id,
            "manifest_sha256": staged_selector.current_manifest_sha256,
            "previous_release_id": staged_selector.previous_release_id,
            "previous_manifest_sha256": staged_selector.previous_manifest_sha256,
            "audited_at": staged_selector.audited_at,
        }
        if reviewed_staged != expected_staged:
            raise PromotionError("staged selector differs from the reviewed activation")

        if initial_selector.current_release_id is None:
            predeploy_release = None
        elif (
            initial_selector.current_release_id == candidate.release_id
            and initial_selector.current_manifest_sha256
            in {None, candidate.manifest_sha256}
        ):
            predeploy_release = candidate
        elif (
            prior is not None
            and initial_selector.current_release_id == prior.release_id
            and initial_selector.current_manifest_sha256
            in {None, prior.manifest_sha256}
        ):
            predeploy_release = prior
        else:
            raise PromotionError(
                "reviewed predeploy selector lacks a verified frozen release"
            )
        planned = intent.get("planned")
        public_result = intent.get("public_result")
        history = intent.get("history")
        if (
            not isinstance(planned, dict)
            or not isinstance(public_result, dict)
            or not isinstance(history, dict)
            or not isinstance(planned.get("tag"), str)
            or not isinstance(planned.get("message"), str)
        ):
            raise PromotionError("activation promoter intent is malformed")
        history_raw = {
            "deployments": self._reviewed_evidence_body(retained, "deployments_history"),
            "versions": self._reviewed_evidence_body(retained, "versions_history"),
        }
        prepared = PreparedPromotion(
            activation.dry_run_attempt_id,
            audited_at,
            planned["tag"],
            planned["message"],
            candidate,
            prior,
            predeploy_release,
            public_baseline,
            initial_selector,
            deployment_before,
            status_before_body,
            status_after_body,
            retained.public_dir,
            inventory_tree(retained.public_dir),
            copy.deepcopy(public_result),
            staged_selector,
            retained.worker_dir,
            retained.worker_entry,
            inventory_tree(retained.worker_dir),
            retained.config,
            sha256_bytes(retained.config.read_bytes()),
            node_version,
            wrangler_version,
            str(intent.get("trust_source")),
            copy.deepcopy(history),
            history_raw=history_raw,
            activation=activation,
            wrangler_installation=wrangler_installation,
            node_executables=node_executables,
        )
        reconstructed = self._intent_payload(prepared)
        reconstructed.pop("activation_bundle", None)
        reviewed_intent = copy.deepcopy(dict(intent))
        reviewed_intent["approval_note"] = self.approval_note
        # Authorization is deliberately bound only after P1B review. These are
        # the only intent fields allowed to differ from the retained dry-run.
        reviewed_intent["first_deploy_approval"] = self.first_deploy_approval
        if reconstructed != reviewed_intent:
            raise PromotionError(
                "retained dry-run intent differs from the executable reviewed activation"
            )
        return prepared

    def prepare(self) -> PreparedPromotion:
        assert (
            self.release_id is not None
            and self.release_root is not None
            and self.public_baseline_id is not None
            and self.public_baseline_root is not None
        )
        candidate = self._verify_release(self.release_id, self.release_root)
        try:
            public_baseline = verify_public_baseline(
                self.public_baseline_root,
                self.repo.resolve(strict=True),
                expected_baseline_id=self.public_baseline_id,
                expected_authority_git_commit=candidate.authority_commit,
            )
        except BaselineValidationError as exc:
            raise PromotionError(f"public asset baseline verification failed: {exc}") from exc
        self._check_git_authority(candidate.authority_commit)
        node_executables = self._node_executables_identity()
        wrangler_installation = self._wrangler_installation_identity()
        self._bind_node_executables(node_executables)
        node_version, wrangler_version = self._verify_toolchain()

        initial_probe, trust_source = self._probe_selector("initial")
        selector = selector_from_probe(
            initial_probe,
            candidate.release_id,
            candidate_manifest_sha256=candidate.manifest_sha256,
            allow_first_deploy=self.allow_first_deploy,
        )
        prior: ReleaseInfo | None = None
        if selector.retained_release_id is not None:
            prior_root_name = (
                selector.retained_manifest_sha256
                or selector.retained_release_id.removeprefix("sha256:")
            )
            prior_root = candidate.root.parent / prior_root_name
            prior = self._verify_release(
                selector.retained_release_id,
                prior_root,
                allow_legacy_release_id_root=(
                    selector.retained_manifest_sha256 is None
                ),
            )
            if (
                selector.retained_manifest_sha256 is not None
                and prior.manifest_sha256 != selector.retained_manifest_sha256
            ):
                raise PromotionError(
                    "retained release manifest differs from the production selector"
                )
        if selector.current_release_id is None:
            predeploy_release = None
        elif (
            selector.current_release_id == candidate.release_id
            and (
                selector.current_manifest_sha256 is None
                or selector.current_manifest_sha256 == candidate.manifest_sha256
            )
        ):
            predeploy_release = candidate
        elif (
            prior is not None
            and selector.current_release_id == prior.release_id
            and (
                selector.current_manifest_sha256 is None
                or selector.current_manifest_sha256 == prior.manifest_sha256
            )
        ):
            predeploy_release = prior
        else:
            raise PromotionError("predeploy selector release is not available as a verified frozen release")

        work_root = Path(
            tempfile.mkdtemp(prefix=f"wikilean-brain-promote-{self.attempt_id}-")
        ).resolve(strict=True)
        if _is_relative_to(work_root, self.repo.resolve(strict=True)):
            remove_sealed_tree(work_root)
            raise PromotionError("temporary promotion workspace must be outside the repository")
        public_dir = work_root / "public"
        bundle_dir = work_root / "bundle"
        deploy_config = work_root / "wrangler.jsonc"
        try:
            shutil.copyfile(self.wiki / "wrangler.jsonc", deploy_config)
            deploy_config_sha256 = sha256_bytes(deploy_config.read_bytes())
            public_result, staged_selector = self._stage_public(
                candidate, prior, public_baseline, public_dir
            )
            before_checks = inventory_tree(public_dir)
            bundle_entry, bundle_before = self._run_worker_checks(
                public_dir, bundle_dir, deploy_config
            )
            after_checks = inventory_tree(public_dir)
            if before_checks != after_checks:
                raise PromotionError("staged public bytes changed during Worker checks")
            seal_tree_read_only(public_dir)
            seal_tree_read_only(bundle_dir)
            os.chmod(deploy_config, 0o400)
            sealed_inventory = inventory_tree(public_dir)
            if sealed_inventory != before_checks:
                raise PromotionError("staged public bytes changed while sealing the upload tree")
            sealed_bundle_inventory = inventory_tree(bundle_dir)
            if sealed_bundle_inventory != bundle_before:
                raise PromotionError("Worker bundle changed while sealing the upload tree")
            if sha256_bytes(deploy_config.read_bytes()) != deploy_config_sha256:
                raise PromotionError("Wrangler configuration changed while sealing deployment inputs")

            candidate_after = self._verify_release(candidate.release_id, candidate.root)
            if candidate_after.tree != candidate.tree or candidate_after.manifest_sha256 != candidate.manifest_sha256:
                raise PromotionError("candidate frozen release changed during preparation")
            if prior is not None:
                prior_after = self._verify_release(
                    prior.release_id,
                    prior.root,
                    allow_legacy_release_id_root=(
                        prior.root.name == prior.release_hex
                    ),
                )
                if prior_after.tree != prior.tree or prior_after.manifest_sha256 != prior.manifest_sha256:
                    raise PromotionError("prior frozen release changed during preparation")
            self._check_git_authority(candidate.authority_commit)

            history, history_raw = self._history_evidence(deploy_config)
            status_before, status_before_result = self._wrangler_status(deploy_config)
            final_probe, final_trust_source = self._probe_selector("final")
            if final_trust_source != trust_source:
                raise PromotionError("selector preflights used different trust configurations")
            if (
                final_probe.status != initial_probe.status
                or final_probe.body != initial_probe.body
            ):
                raise PromotionError("production selector changed during staging/checks")
            status_after, status_after_result = self._wrangler_status(deploy_config)
            if not self._same_deployment(status_before, status_after):
                raise PromotionError("Worker deployment changed during the prestate sandwich")
            tag = f"brain-{candidate.release_hex[:12]}-{self.attempt_id[-10:]}"
            message = f"Brain release {candidate.release_id} attempt {self.attempt_id}"
            return PreparedPromotion(
                self.attempt_id,
                self.audited_at,
                tag,
                message,
                candidate,
                prior,
                predeploy_release,
                public_baseline,
                selector,
                status_before,
                status_before_result.stdout,
                status_after_result.stdout,
                public_dir,
                sealed_inventory,
                public_result,
                staged_selector,
                bundle_dir,
                bundle_entry,
                sealed_bundle_inventory,
                deploy_config,
                deploy_config_sha256,
                node_version,
                wrangler_version,
                trust_source,
                history,
                history_raw=history_raw,
                wrangler_installation=wrangler_installation,
                node_executables=node_executables,
            )
        except BaseException:
            remove_sealed_tree(work_root)
            raise

    @staticmethod
    def _same_deployment(left: DeploymentState, right: DeploymentState) -> bool:
        return (
            left.deployment_id == right.deployment_id
            and left.version_id == right.version_id
        )

    def _version_belongs_to_attempt(
        self,
        version_id: str,
        tag: str,
        message: str,
        config: Path | None = None,
    ) -> tuple[bool, dict[str, str], RunResult]:
        annotations, result = self._wrangler_version_annotations(version_id, config)
        belongs = (
            annotations.get("workers/tag") == tag
            and annotations.get("workers/message") == message
        )
        return belongs, annotations, result

    def _wait_for_attempt_version(
        self,
        *,
        tag: str,
        message: str,
        hinted_version: str | None,
        config: Path | None = None,
    ) -> tuple[DeploymentState | None, dict[str, str] | None, list[dict[str, object]]]:
        observations: list[dict[str, object]] = []
        for attempt in range(1, self.status_attempts + 1):
            try:
                state, status_result = self._wrangler_status(config)
                belongs, annotations, view_result = self._version_belongs_to_attempt(
                    state.version_id, tag, message, config
                )
                observations.append(
                    {
                        "attempt": attempt,
                        "deployment_id": state.deployment_id,
                        "version_id": state.version_id,
                        "belongs_to_attempt": belongs,
                        "status_sha256": sha256_bytes(status_result.stdout),
                        "version_view_sha256": sha256_bytes(view_result.stdout),
                    }
                )
                if belongs:
                    if hinted_version is not None and hinted_version != state.version_id:
                        raise PromotionError(
                            "Wrangler output candidate differs from the attempt-correlated live version"
                        )
                    return state, annotations, observations
            except PromotionError as exc:
                observations.append({"attempt": attempt, "error": str(exc)})
            if attempt < self.status_attempts:
                self.sleep(self.status_interval)
        return None, None, observations

    def _wait_for_exact_version(
        self,
        version_id: str,
        *,
        previous_deployment_id: str | None = None,
        config: Path | None = None,
    ) -> tuple[DeploymentState | None, list[dict[str, object]]]:
        observations: list[dict[str, object]] = []
        for attempt in range(1, self.status_attempts + 1):
            try:
                state, result = self._wrangler_status(config)
                changed_deployment = (
                    previous_deployment_id is None or state.deployment_id != previous_deployment_id
                )
                observations.append(
                    {
                        "attempt": attempt,
                        "deployment_id": state.deployment_id,
                        "version_id": state.version_id,
                        "status_sha256": sha256_bytes(result.stdout),
                        "new_deployment": changed_deployment,
                    }
                )
                if state.version_id == version_id and changed_deployment:
                    return state, observations
            except PromotionError as exc:
                observations.append({"attempt": attempt, "error": str(exc)})
            if attempt < self.status_attempts:
                self.sleep(self.status_interval)
        return None, observations

    def _run_canary(
        self,
        release_id: str,
        manifest_sha256: str,
        journal: EventJournal,
        prefix: str,
        expected_trust_source: str,
        public_baseline: PublicAssetBaseline | None = None,
    ) -> tuple[bool, dict[str, object], RunResult]:
        command = [
            str(self.python),
            str(self.repo / "site" / "ops" / "brain-canary.py"),
            "--base-url",
            self.base_url,
            "--expected-release-id",
            release_id,
            "--expected-manifest-sha256",
            manifest_sha256,
            "--timeout",
            str(self.canary_timeout),
            "--interval",
            str(self.canary_interval),
            "--max-response-bytes",
            str(self.canary_max_response_bytes),
        ]
        if public_baseline is not None:
            command.extend(
                [
                    "--public-baseline-id",
                    public_baseline.baseline_id,
                    "--public-baseline-root",
                    str(public_baseline.root),
                ]
            )
        result = self._run(
            command,
            cwd=self.repo,
            timeout=max(self.command_timeout, self.canary_timeout + 60),
        )
        evidence = command_evidence(result, journal, prefix)
        candidates = [body for body in (result.stdout, result.stderr) if body.strip()]
        parsed: object = {}
        for body in candidates:
            try:
                parsed = extract_last_json_value(body, f"{prefix} canary")
                break
            except PromotionError:
                continue
        if not isinstance(parsed, dict):
            parsed = {"ok": False, "error": "canary emitted no result object"}
        payload = {
            "expected_release_id": release_id,
            "expected_manifest_sha256": manifest_sha256,
            "expected_trust_source": expected_trust_source,
            "expected_public_baseline_id": (
                public_baseline.baseline_id if public_baseline is not None else None
            ),
            "result": parsed,
            "command": evidence,
        }
        ok = (
            result.ok
            and parsed.get("ok") is True
            and parsed.get("release_id") == release_id
            and parsed.get("manifest_sha256") == manifest_sha256
            and parsed.get("trust_source") == expected_trust_source
            and (
                public_baseline is None
                or (
                    parsed.get("public_baseline_id") == public_baseline.baseline_id
                    and isinstance(parsed.get("public_assets_checked"), list)
                    and bool(parsed.get("public_assets_checked"))
                )
            )
        )
        return ok, payload, result

    def _observe_expected_release(
        self,
        *,
        expected_release_id: str,
        expected_manifest_sha256: str,
        expected_version_id: str,
        expected_previous_release_id: str | None,
        expected_previous_manifest_sha256: str | None,
        expected_selector_sha256: str,
        expected_audited_at: str | None,
        expected_trust_source: str,
        expected_attempt_tag: str | None = None,
        expected_attempt_message: str | None = None,
        require_new_deployment_from: str | None = None,
        config: Path | None = None,
    ) -> dict[str, object]:
        before, before_result = self._wrangler_status(config)
        if before.version_id != expected_version_id:
            raise PromotionError(
                f"live Worker version {before.version_id} does not equal expected {expected_version_id}"
            )
        if require_new_deployment_from is not None and before.deployment_id == require_new_deployment_from:
            raise PromotionError("expected a new Wrangler deployment ID but production did not change")
        annotations: dict[str, str] | None = None
        version_view_sha: str | None = None
        if expected_attempt_tag is not None and expected_attempt_message is not None:
            belongs, annotations, view_result = self._version_belongs_to_attempt(
                before.version_id, expected_attempt_tag, expected_attempt_message, config
            )
            if not belongs:
                raise PromotionError("live Worker version is not annotated for this promotion attempt")
            version_view_sha = sha256_bytes(view_result.stdout)
        probe, trust_source = self._probe_selector("observe")
        if trust_source != expected_trust_source:
            raise PromotionError("live selector used a different TLS trust configuration")
        selector = selector_from_probe(
            probe,
            expected_release_id,
            candidate_manifest_sha256=expected_manifest_sha256,
            allow_first_deploy=False,
        )
        if selector.current_release_id != expected_release_id:
            raise PromotionError(
                f"live selector names {selector.current_release_id}, expected {expected_release_id}"
            )
        if selector.current_manifest_sha256 != expected_manifest_sha256:
            raise PromotionError(
                "live selector manifest digest differs from the staged candidate"
            )
        if selector.previous_release_id != expected_previous_release_id:
            raise PromotionError(
                "live selector previous release does not equal the staged retention target"
            )
        if (
            selector.previous_manifest_sha256
            != expected_previous_manifest_sha256
        ):
            raise PromotionError(
                "live selector previous manifest differs from the staged retention target"
            )
        if selector.body_sha256 != expected_selector_sha256:
            raise PromotionError("live selector bytes differ from the staged selector")
        if selector.audited_at != expected_audited_at:
            raise PromotionError("live selector audited_at differs from the staged selector")
        after, after_result = self._wrangler_status(config)
        if not self._same_deployment(before, after):
            raise PromotionError("Worker deployment changed during final observation sandwich")
        return {
            "deployment_id": before.deployment_id,
            "version_id": before.version_id,
            "selector_status": selector.status,
            "selector_sha256": selector.body_sha256,
            "release_id": selector.current_release_id,
            "manifest_sha256": selector.current_manifest_sha256,
            "previous_manifest_sha256": selector.previous_manifest_sha256,
            "status_before_sha256": sha256_bytes(before_result.stdout),
            "status_after_sha256": sha256_bytes(after_result.stdout),
            "version_annotations": annotations,
            "version_view_sha256": version_view_sha,
            "trust_source": trust_source,
        }

    def _intent_payload(self, prepared: PreparedPromotion) -> dict[str, object]:
        payload: dict[str, object] = {
            "requested_release_id": prepared.candidate.release_id,
            "release_root": str(prepared.candidate.root),
            "release_manifest_sha256": prepared.candidate.manifest_sha256,
            "release_tree": prepared.candidate.tree,
            "authority_commit": prepared.candidate.authority_commit,
            "reducer_commit": prepared.candidate.reducer_commit,
            "retained_release": (
                {
                    "release_id": prepared.prior.release_id,
                    "release_root": str(prepared.prior.root),
                    "release_manifest_sha256": prepared.prior.manifest_sha256,
                    "release_tree": prepared.prior.tree,
                    "authority_commit": prepared.prior.authority_commit,
                    "reducer_commit": prepared.prior.reducer_commit,
                }
                if prepared.prior is not None
                else None
            ),
            "public_baseline": {
                "baseline_id": prepared.public_baseline.baseline_id,
                "root": str(prepared.public_baseline.root),
                "manifest": str(prepared.public_baseline.manifest_path),
                "manifest_sha256": sha256_bytes(
                    prepared.public_baseline.manifest_path.read_bytes()
                ),
                "authority_commit": prepared.public_baseline.authority_git_commit,
                "files": len(prepared.public_baseline.files),
                "bytes": prepared.public_baseline.total_bytes,
            },
            "public_tree": prepared.public_inventory,
            "public_result": prepared.public_result,
            "staged_selector": {
                "sha256": prepared.staged_selector.body_sha256,
                "release_id": prepared.staged_selector.current_release_id,
                "manifest_sha256": prepared.staged_selector.current_manifest_sha256,
                "previous_release_id": prepared.staged_selector.previous_release_id,
                "previous_manifest_sha256": (
                    prepared.staged_selector.previous_manifest_sha256
                ),
                "audited_at": prepared.staged_selector.audited_at,
            },
            "worker_bundle": {
                "tree": prepared.bundle_inventory,
                "entry": str(prepared.bundle_entry),
                "config": str(prepared.deploy_config),
                "config_sha256": prepared.deploy_config_sha256,
                "node_version": prepared.node_version,
                "wrangler_version": prepared.wrangler_version,
                "installation": prepared.wrangler_installation,
                "executables": prepared.node_executables,
            },
            "audited_at": prepared.audited_at,
            "base_url": self.base_url,
            "trust_source": prepared.trust_source,
            "predeploy": {
                "deployment_id": prepared.predeploy.deployment_id,
                "version_id": prepared.predeploy.version_id,
                "status_sha256": prepared.predeploy.raw_sha256,
                "selector_status": prepared.initial_selector.status,
                "selector_sha256": prepared.initial_selector.body_sha256,
                "release_id": prepared.initial_selector.current_release_id,
                "manifest_sha256": prepared.initial_selector.current_manifest_sha256,
                "previous_release_id": prepared.initial_selector.previous_release_id,
                "previous_manifest_sha256": (
                    prepared.initial_selector.previous_manifest_sha256
                ),
                "audited_at": prepared.initial_selector.audited_at,
            },
            "planned": {
                "tag": prepared.tag,
                "message": prepared.message,
                "command_timeout_seconds": format(self.command_timeout, ".17g"),
            },
            "approval_note": self.approval_note,
            "first_deploy_exception": self.allow_first_deploy,
            "first_deploy_approval": self.first_deploy_approval,
            "history": prepared.history,
        }
        if prepared.activation is not None:
            payload["retained_artifacts"] = prepared.activation.retained.reference()
            payload["activation_bundle"] = prepared.activation.reference()
        return payload

    def _retained_intent_payload(
        self,
        prepared: PreparedPromotion,
        retained: RetainedDryRunArtifacts,
    ) -> dict[str, object]:
        payload = copy.deepcopy(self._intent_payload(prepared))
        payload["public_tree"] = inventory_tree(retained.public_dir)
        public_result = payload.get("public_result")
        if not isinstance(public_result, dict):
            raise PromotionError("public staging result is unavailable for retained dry-run output")
        public_result["public_dir"] = str(retained.public_dir)
        brain = public_result.get("brain")
        if not isinstance(brain, dict):
            raise PromotionError("Brain staging result is unavailable for retained dry-run output")
        brain["destination"] = str(retained.public_dir / "assets" / "brain")
        page = brain.get("brain_page")
        if not isinstance(page, dict):
            raise PromotionError("Brain page evidence is unavailable for retained dry-run output")
        page["destination"] = str(retained.public_dir / "brain.html")

        worker = payload.get("worker_bundle")
        if not isinstance(worker, dict):
            raise PromotionError("Worker bundle evidence is unavailable for retained dry-run output")
        worker["tree"] = inventory_tree(retained.worker_dir)
        worker["entry"] = str(retained.worker_entry)
        worker["config"] = str(retained.config)
        worker["config_sha256"] = sha256_bytes(retained.config.read_bytes())
        payload["retained_artifacts"] = retained.reference()
        return payload

    def _final_predeploy_fence(
        self,
        prepared: PreparedPromotion,
        journal: EventJournal,
    ) -> dict[str, object]:
        if prepared.activation is None:
            raise PromotionError("deployment has no reviewed activation bundle")
        self._verify_reviewed_activation(prepared.activation)
        if prepared.public_dir.is_symlink() or prepared.public_dir.resolve(strict=True) != prepared.public_dir:
            raise PromotionError("sealed public directory identity changed before deployment")
        if prepared.bundle_dir.is_symlink() or prepared.bundle_dir.resolve(strict=True) != prepared.bundle_dir:
            raise PromotionError("sealed bundle directory identity changed before deployment")
        if prepared.deploy_config.is_symlink() or not prepared.deploy_config.is_file():
            raise PromotionError("sealed Wrangler configuration is unavailable")
        if prepared.bundle_entry.is_symlink() or not prepared.bundle_entry.is_file():
            raise PromotionError("sealed Worker bundle entry is unavailable")
        public_now = inventory_tree(prepared.public_dir)
        bundle_now = inventory_tree(prepared.bundle_dir)
        config_now = sha256_bytes(prepared.deploy_config.read_bytes())
        if public_now != prepared.public_inventory:
            raise PromotionError("sealed public tree changed after durable intent")
        if bundle_now != prepared.bundle_inventory:
            raise PromotionError("sealed Worker bundle changed after durable intent")
        if config_now != prepared.deploy_config_sha256:
            raise PromotionError("sealed Wrangler configuration changed after durable intent")

        try:
            baseline_now = verify_public_baseline(
                prepared.public_baseline.root,
                self.repo.resolve(strict=True),
                expected_baseline_id=prepared.public_baseline.baseline_id,
                expected_authority_git_commit=prepared.candidate.authority_commit,
            )
        except BaselineValidationError as exc:
            raise PromotionError(f"public asset baseline changed after durable intent: {exc}") from exc
        if baseline_now != prepared.public_baseline:
            raise PromotionError("public asset baseline identity changed after durable intent")

        candidate_now = self._verify_release(prepared.candidate.release_id, prepared.candidate.root)
        if (
            candidate_now.tree != prepared.candidate.tree
            or candidate_now.manifest_sha256 != prepared.candidate.manifest_sha256
        ):
            raise PromotionError("candidate frozen release changed after durable intent")
        if prepared.prior is not None:
            prior_now = self._verify_release(
                prepared.prior.release_id,
                prepared.prior.root,
                allow_legacy_release_id_root=(
                    prepared.prior.root.name == prepared.prior.release_hex
                ),
            )
            if (
                prior_now.tree != prepared.prior.tree
                or prior_now.manifest_sha256 != prepared.prior.manifest_sha256
            ):
                raise PromotionError("retained frozen release changed after durable intent")
        installation = self._wrangler_installation_identity()
        if installation != prepared.wrangler_installation:
            raise PromotionError(
                "installed Wrangler dependency tree changed after review"
            )
        executables = self._node_executables_identity()
        if executables != prepared.node_executables:
            raise PromotionError("Node/Wrangler executable bytes changed after review")
        self._bind_node_executables(executables)
        node_version, wrangler_version = self._verify_toolchain()
        if (
            node_version != prepared.node_version
            or wrangler_version != prepared.wrangler_version
        ):
            raise PromotionError("Node/Wrangler identity changed after preparation")
        self._check_git_authority(prepared.candidate.authority_commit)

        before, before_result, probe, after, after_result = self._remote_predeploy_fence(
            prepared,
            config=prepared.deploy_config,
            phase="recorded-last-fence",
        )
        trust_source = getattr(probe, "trust_source", prepared.trust_source)
        evidence = {
            "phase": "last_predeploy_fence",
            "ok": True,
            "production_mutated": False,
            "public_tree": public_now,
            "bundle_tree": bundle_now,
            "config_sha256": config_now,
            "node_version": node_version,
            "wrangler_version": wrangler_version,
            "selector": journal.append_blob(
                "last-predeploy-selector",
                probe.body,
                "application/json" if probe.status == 200 else "application/octet-stream",
            ),
            "selector_status": probe.status,
            "selector_sha256": probe.sha256,
            "status_before": journal.append_blob(
                "last-predeploy-status-before", before_result.stdout, "application/json"
            ),
            "status_after": journal.append_blob(
                "last-predeploy-status-after", after_result.stdout, "application/json"
            ),
            "trust_source": trust_source,
        }
        append_event(journal, "observation", evidence)
        return evidence

    def _remote_predeploy_fence(
        self,
        prepared: PreparedPromotion,
        *,
        config: Path,
        phase: str,
    ) -> tuple[DeploymentState, RunResult, SelectorProbe, DeploymentState, RunResult]:
        before, before_result = self._wrangler_status(config)
        probe, trust_source = self._probe_selector(phase)
        after, after_result = self._wrangler_status(config)
        selector_matches = (
            probe.status == prepared.initial_selector.status
            and probe.body == prepared.initial_selector.body
        )
        if trust_source != prepared.trust_source:
            raise PromotionError("selector trust configuration changed before deployment")
        if not selector_matches:
            raise PromotionError("production selector changed before deployment")
        if (
            not self._same_deployment(before, after)
            or not self._same_deployment(before, prepared.predeploy)
        ):
            raise PromotionError("Worker deployment changed before deployment")
        return before, before_result, probe, after, after_result

    def _deploy(self, prepared: PreparedPromotion, journal: EventJournal) -> int:
        if prepared.activation is None:
            raise PromotionError("deployment requires a reviewed activation bundle")
        if prepared.node_executables is None:
            raise PromotionError("deployment requires reviewed Node/Wrangler executables")
        self._bind_node_executables(prepared.node_executables)
        append_event(
            journal,
            "observation",
            {
                "phase": "predeploy_evidence",
                "activation_bundle": prepared.activation.reference(),
                "selector": journal.append_blob(
                    "predeploy-selector",
                    prepared.initial_selector.body,
                    "application/json" if prepared.initial_selector.status == 200 else "application/octet-stream",
                ),
                "status_before": journal.append_blob(
                    "predeploy-status-before",
                    prepared.predeploy_status_before,
                    "application/json",
                ),
                "status_after": journal.append_blob(
                    "predeploy-status-after",
                    prepared.predeploy_status_after,
                    "application/json",
                ),
                "worker_bundle": journal.append_blob(
                    "worker-bundle-index-js",
                    prepared.bundle_entry.read_bytes(),
                    "text/javascript; charset=utf-8",
                ),
                "wrangler_config": journal.append_blob(
                    "wrangler-config",
                    prepared.deploy_config.read_bytes(),
                    "application/jsonc; charset=utf-8",
                ),
            },
        )
        command = self._wrangler_command(
            "deploy",
            str(prepared.bundle_entry),
            "--config",
            str(prepared.deploy_config),
            "--no-bundle",
            "--strict",
            "--assets",
            str(prepared.public_dir),
            "--tag",
            prepared.tag,
            "--message",
            prepared.message,
        )
        append_event(
            journal,
            "deploy_invocation",
            {
                "command": command,
                "activation_bundle": prepared.activation.reference(),
                "production_mutation_possible_after_this_event": True,
            },
        )
        try:
            self._final_predeploy_fence(prepared, journal)
            # This final fence performs no journal or filesystem writes. Recheck
            # the reviewed tool bytes after its status calls, then spawn Wrangler.
            self._remote_predeploy_fence(
                prepared,
                config=prepared.deploy_config,
                phase="immediate-before-deploy",
            )
            if self._wrangler_installation_identity() != prepared.wrangler_installation:
                raise PromotionError(
                    "installed Wrangler dependency tree changed during the immediate predeploy fence"
                )
            if self._node_executables_identity() != prepared.node_executables:
                raise PromotionError(
                    "Node/Wrangler executable bytes changed during the immediate predeploy fence"
                )
        except PromotionError as exc:
            append_event(
                journal,
                "observation",
                {
                    "phase": "last_predeploy_fence",
                    "ok": False,
                    "production_mutated": False,
                    "error": str(exc),
                },
            )
            append_event(
                journal,
                "final_state",
                {
                    "outcome": "predeploy_race_aborted",
                    "release_id": prepared.initial_selector.current_release_id,
                    "deployment_id": prepared.predeploy.deployment_id,
                    "version_id": prepared.predeploy.version_id,
                    "production_mutated": False,
                },
            )
            print(
                json.dumps(
                    {
                        "schema": RESULT_SCHEMA,
                        "ok": False,
                        "attempt_id": self.attempt_id,
                        "outcome": "predeploy_race_aborted",
                        "error": str(exc),
                        "production_mutated": False,
                        "journal_tip": journal.chain_tip,
                    },
                    sort_keys=True,
                ),
                file=sys.stderr,
            )
            return 1
        deploy_result = self._run(
            command,
            cwd=self.wiki,
            timeout=self.command_timeout,
            env=self._reviewed_node_environment(include_cloudflare=True),
        )
        combined = deploy_result.stdout + b"\n" + deploy_result.stderr
        candidate_hint = parse_candidate_version(combined)
        append_event(journal,
            "deploy_result",
            {
                "candidate_version_hint": candidate_hint,
                "command": command_evidence(deploy_result, journal, "deploy"),
            },
        )

        live_candidate, annotations, convergence = self._wait_for_attempt_version(
            tag=prepared.tag,
            message=prepared.message,
            hinted_version=candidate_hint,
            config=prepared.deploy_config,
        )
        append_event(journal,
            "observation",
            {
                "phase": "post_deploy_control_plane",
                "candidate": (
                    {
                        "deployment_id": live_candidate.deployment_id,
                        "version_id": live_candidate.version_id,
                        "annotations": annotations,
                    }
                    if live_candidate is not None
                    else None
                ),
                "polls": convergence,
            },
        )

        canary_ok, canary_payload, _ = self._run_canary(
            prepared.candidate.release_id,
            prepared.candidate.manifest_sha256,
            journal,
            "candidate",
            prepared.trust_source,
            prepared.public_baseline,
        )
        append_event(journal, "canary_result", canary_payload)

        if live_candidate is not None and canary_ok:
            try:
                final_observation = self._observe_expected_release(
                    expected_release_id=prepared.candidate.release_id,
                    expected_manifest_sha256=prepared.candidate.manifest_sha256,
                    expected_version_id=live_candidate.version_id,
                    expected_previous_release_id=prepared.staged_selector.previous_release_id,
                    expected_previous_manifest_sha256=(
                        prepared.staged_selector.previous_manifest_sha256
                    ),
                    expected_selector_sha256=prepared.staged_selector.body_sha256,
                    expected_audited_at=prepared.staged_selector.audited_at,
                    expected_trust_source=prepared.trust_source,
                    expected_attempt_tag=prepared.tag,
                    expected_attempt_message=prepared.message,
                    config=prepared.deploy_config,
                )
            except PromotionError as exc:
                append_event(journal,
                    "observation",
                    {"phase": "post_canary", "ok": False, "error": str(exc)},
                )
            else:
                append_event(journal,
                    "observation",
                    {"phase": "post_canary", "ok": True, **final_observation},
                )
                outcome = "deployed" if deploy_result.ok else "deployed_after_uncertain_command"
                final = append_event(journal,
                    "final_state",
                    {
                        "outcome": outcome,
                        "release_id": prepared.candidate.release_id,
                        "deployment_id": live_candidate.deployment_id,
                        "version_id": live_candidate.version_id,
                    },
                )
                print(
                    json.dumps(
                        {
                            "schema": RESULT_SCHEMA,
                            "ok": True,
                            "attempt_id": self.attempt_id,
                            "outcome": outcome,
                            "release_id": prepared.candidate.release_id,
                            "deployment_id": live_candidate.deployment_id,
                            "version_id": live_candidate.version_id,
                            "journal_tip": getattr(journal, "chain_tip", None),
                            "final_event": final,
                        },
                        sort_keys=True,
                    )
                )
                return 0

        append_event(journal,
            "observation",
            {
                "phase": "manual_intervention_required",
                "reason": (
                    "candidate content canary failed"
                    if not canary_ok
                    else "candidate ownership/final state could not be proven"
                ),
                "candidate_version": live_candidate.version_id if live_candidate else None,
            },
        )
        print(
            json.dumps(
                {
                    "schema": RESULT_SCHEMA,
                    "ok": False,
                    "attempt_id": self.attempt_id,
                    "outcome": "manual_intervention_required",
                    "error": "promotion remains incomplete; reconcile it before another mutation",
                    "journal_tip": getattr(journal, "chain_tip", None),
                },
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return 1

    @staticmethod
    def _journal_intent(journal: EventJournal) -> dict[str, object]:
        events = journal.events
        if not events or events[0].get("kind") != "intent":
            raise PromotionError("incomplete journal has no valid intent event")
        payload = events[0].get("payload")
        if not isinstance(payload, dict):
            raise PromotionError("journal intent payload is malformed")
        return payload

    def _stable_reconciliation_observation(
        self,
        config: Path | None = None,
    ) -> ReconciliationObservation:
        before, before_result = self._wrangler_status(config)
        probe, trust_source = self._probe_selector("reconcile")
        selector: SelectorState | None
        if probe.status == 404:
            selector = None
        elif probe.status == 200:
            value = _decode_json(probe.body, "reconciliation selector")
            if not isinstance(value, dict):
                raise PromotionError("reconciliation selector must be an object")
            release_id = value.get("release_id")
            if not isinstance(release_id, str) or RELEASE_ID_RE.fullmatch(release_id) is None:
                raise PromotionError("reconciliation selector has no valid release ID")
            manifest_sha256 = value.get("manifest_sha256")
            if manifest_sha256 is not None and not isinstance(manifest_sha256, str):
                raise PromotionError("reconciliation selector manifest digest is malformed")
            selector = selector_from_probe(
                probe,
                release_id,
                candidate_manifest_sha256=manifest_sha256,
                allow_first_deploy=False,
            )
        else:
            raise PromotionError(f"reconciliation selector returned HTTP {probe.status}")
        after, after_result = self._wrangler_status(config)
        if not self._same_deployment(before, after):
            raise PromotionError("Worker deployment changed during reconciliation sandwich")
        annotations, _ = self._wrangler_version_annotations(before.version_id, config)
        return ReconciliationObservation(
            before,
            probe,
            selector,
            annotations,
            trust_source,
            before_result.stdout,
            after_result.stdout,
        )

    @staticmethod
    def _same_reconciliation_state(
        left: ReconciliationObservation,
        right: ReconciliationObservation,
    ) -> bool:
        selector_same = (
            left.selector_probe.status == right.selector_probe.status
            and left.selector_probe.body == right.selector_probe.body
        )
        return (
            BrainPromoter._same_deployment(left.deployment, right.deployment)
            and selector_same
            and left.annotations == right.annotations
            and left.trust_source == right.trust_source
        )

    @staticmethod
    def _release_matches_intent(
        release: ReleaseInfo,
        *,
        manifest_sha256: object,
        tree: object,
        authority_commit: object,
        reducer_commit: object,
    ) -> bool:
        return (
            release.manifest_sha256 == manifest_sha256
            and release.tree == tree
            and release.authority_commit == authority_commit
            and release.reducer_commit == reducer_commit
        )

    def _record_reconciliation_observation(
        self,
        journal: EventJournal,
        observation: ReconciliationObservation,
        *,
        phase: str,
    ) -> None:
        append_event(
            journal,
            "observation",
            {
                "phase": phase,
                "approval_note": self.approval_note,
                "deployment_id": observation.deployment.deployment_id,
                "version_id": observation.deployment.version_id,
                "selector_status": observation.selector_probe.status,
                "selector_sha256": observation.selector_probe.sha256,
                "release_id": (
                    observation.selector.current_release_id
                    if observation.selector is not None
                    else None
                ),
                "manifest_sha256": (
                    observation.selector.current_manifest_sha256
                    if observation.selector is not None
                    else None
                ),
                "previous_release_id": (
                    observation.selector.previous_release_id
                    if observation.selector is not None
                    else None
                ),
                "previous_manifest_sha256": (
                    observation.selector.previous_manifest_sha256
                    if observation.selector is not None
                    else None
                ),
                "audited_at": (
                    observation.selector.audited_at
                    if observation.selector is not None
                    else None
                ),
                "trust_source": observation.trust_source,
                "version_annotations": observation.annotations,
                "selector": journal.append_blob(
                    f"{phase}-selector",
                    observation.selector_probe.body,
                    (
                        "application/json"
                        if observation.selector_probe.status == 200
                        else "application/octet-stream"
                    ),
                ),
                "status_before": journal.append_blob(
                    f"{phase}-status-before", observation.status_before, "application/json"
                ),
                "status_after": journal.append_blob(
                    f"{phase}-status-after", observation.status_after, "application/json"
                ),
            },
        )

    def reconcile(self, journal: EventJournal) -> int:
        intent = self._journal_intent(journal)
        requested_release = intent.get("requested_release_id")
        release_root = intent.get("release_root")
        release_manifest_sha256 = intent.get("release_manifest_sha256")
        release_tree = intent.get("release_tree")
        authority_commit = intent.get("authority_commit")
        reducer_commit = intent.get("reducer_commit")
        retained_release = intent.get("retained_release")
        public_baseline = intent.get("public_baseline")
        staged_selector = intent.get("staged_selector")
        worker_bundle = intent.get("worker_bundle")
        expected_trust_source = intent.get("trust_source")
        intent_base_url = intent.get("base_url")
        planned = intent.get("planned")
        predeploy = intent.get("predeploy")
        if (
            not isinstance(requested_release, str)
            or RELEASE_ID_RE.fullmatch(requested_release) is None
            or not isinstance(release_root, str)
            or not isinstance(release_manifest_sha256, str)
            or not isinstance(release_tree, dict)
            or not isinstance(authority_commit, str)
            or GIT_COMMIT_RE.fullmatch(authority_commit) is None
            or not isinstance(reducer_commit, str)
            or reducer_commit != authority_commit
            or not isinstance(public_baseline, dict)
            or not isinstance(staged_selector, dict)
            or not isinstance(worker_bundle, dict)
            or not isinstance(expected_trust_source, str)
            or intent_base_url != self.base_url
            or not isinstance(planned, dict)
            or not isinstance(predeploy, dict)
        ):
            raise PromotionError("journal intent lacks verified release/deployment state")
        candidate = self._verify_release(
            requested_release,
            Path(release_root),
            allow_legacy_release_id_root=True,
        )
        if not self._release_matches_intent(
            candidate,
            manifest_sha256=release_manifest_sha256,
            tree=release_tree,
            authority_commit=authority_commit,
            reducer_commit=reducer_commit,
        ):
            raise PromotionError("candidate release no longer matches the durable intent")
        baseline_id = public_baseline.get("baseline_id")
        baseline_root = public_baseline.get("root")
        baseline_manifest_sha256 = public_baseline.get("manifest_sha256")
        if (
            not isinstance(baseline_id, str)
            or RELEASE_ID_RE.fullmatch(baseline_id) is None
            or not isinstance(baseline_root, str)
            or not isinstance(baseline_manifest_sha256, str)
        ):
            raise PromotionError("journal public baseline identity is malformed")
        try:
            verified_baseline = verify_public_baseline(
                Path(baseline_root),
                self.repo.resolve(strict=True),
                expected_baseline_id=baseline_id,
                expected_authority_git_commit=authority_commit,
            )
        except BaselineValidationError as exc:
            raise PromotionError(f"journal public baseline no longer verifies: {exc}") from exc
        if (
            sha256_bytes(verified_baseline.manifest_path.read_bytes())
            != baseline_manifest_sha256
            or public_baseline.get("authority_commit") != authority_commit
            or public_baseline.get("files") != len(verified_baseline.files)
            or public_baseline.get("bytes") != verified_baseline.total_bytes
        ):
            raise PromotionError("journal public baseline differs from its durable intent")
        retained: ReleaseInfo | None = None
        if retained_release is not None:
            if not isinstance(retained_release, dict):
                raise PromotionError("journal retained release is malformed")
            retained_id = retained_release.get("release_id")
            retained_root = retained_release.get("release_root")
            if (
                not isinstance(retained_id, str)
                or RELEASE_ID_RE.fullmatch(retained_id) is None
                or not isinstance(retained_root, str)
            ):
                raise PromotionError("journal retained release identity is malformed")
            retained = self._verify_release(
                retained_id,
                Path(retained_root),
                allow_legacy_release_id_root=True,
            )
            retained_authority = retained_release.get("authority_commit")
            retained_reducer = retained_release.get("reducer_commit")
            if not self._release_matches_intent(
                retained,
                manifest_sha256=retained_release.get("release_manifest_sha256"),
                tree=retained_release.get("release_tree"),
                authority_commit=retained_authority,
                reducer_commit=retained_reducer,
            ):
                raise PromotionError("retained release no longer matches the durable intent")
        self._check_recovery_checkout(authority_commit)
        reviewed_installation = worker_bundle.get("installation")
        if (
            reviewed_installation is not None
            and self._wrangler_installation_identity() != reviewed_installation
        ):
            raise PromotionError(
                "reconciliation Wrangler dependency tree differs from the durable intent"
            )
        reviewed_executables = worker_bundle.get("executables")
        current_executables = self._node_executables_identity()
        if reviewed_executables is not None and current_executables != reviewed_executables:
            raise PromotionError(
                "reconciliation Node/Wrangler executables differ from the durable intent"
            )
        self._bind_node_executables(current_executables)
        node_version, wrangler_version = self._verify_toolchain()
        if (
            worker_bundle.get("node_version") != node_version
            or worker_bundle.get("wrangler_version") != wrangler_version
        ):
            raise PromotionError("reconciliation toolchain differs from the durable intent")
        reconcile_config = self.wiki / "wrangler.jsonc"
        expected_config_sha256 = worker_bundle.get("config_sha256")
        if (
            reconcile_config.is_symlink()
            or not reconcile_config.is_file()
            or not isinstance(expected_config_sha256, str)
            or sha256_bytes(reconcile_config.read_bytes()) != expected_config_sha256
        ):
            raise PromotionError("reconciliation Wrangler configuration differs from intent")

        tag = planned.get("tag")
        message = planned.get("message")
        prior_deployment = predeploy.get("deployment_id")
        prior_version = predeploy.get("version_id")
        prior_release = predeploy.get("release_id")
        prior_manifest_sha256 = predeploy.get("manifest_sha256")
        prior_selector_status = predeploy.get("selector_status")
        prior_selector_sha256 = predeploy.get("selector_sha256")
        prior_previous_release = predeploy.get("previous_release_id")
        prior_previous_manifest_sha256 = predeploy.get("previous_manifest_sha256")
        prior_audited_at = predeploy.get("audited_at")
        expected_selector_sha256 = staged_selector.get("sha256")
        expected_manifest_sha256 = staged_selector.get("manifest_sha256")
        expected_previous_release = staged_selector.get("previous_release_id")
        expected_previous_manifest_sha256 = staged_selector.get(
            "previous_manifest_sha256"
        )
        staged_has_manifest_identity = "manifest_sha256" in staged_selector
        if staged_has_manifest_identity != (
            "previous_manifest_sha256" in staged_selector
        ):
            raise PromotionError(
                "journal staged selector manifest identity is incomplete"
            )
        expected_audited_at = staged_selector.get("audited_at")
        command_timeout_raw = planned.get("command_timeout_seconds")
        if (
            not isinstance(tag, str)
            or not isinstance(message, str)
            or not isinstance(prior_deployment, str)
            or UUID_RE.fullmatch(prior_deployment) is None
            or not isinstance(prior_version, str)
            or UUID_RE.fullmatch(prior_version) is None
            or prior_selector_status not in {200, 404}
            or not isinstance(prior_selector_sha256, str)
            or re.fullmatch(r"[0-9a-f]{64}", prior_selector_sha256) is None
            or not isinstance(expected_selector_sha256, str)
            or re.fullmatch(r"[0-9a-f]{64}", expected_selector_sha256) is None
            or staged_selector.get("release_id") != requested_release
            or not isinstance(expected_audited_at, str)
            or not expected_audited_at
        ):
            raise PromotionError("journal intent predeploy identity is malformed")
        if staged_has_manifest_identity:
            if expected_manifest_sha256 != candidate.manifest_sha256:
                raise PromotionError(
                    "journal staged selector names the wrong candidate manifest"
                )
        elif expected_manifest_sha256 is not None:
            raise PromotionError("legacy journal staged manifest identity is malformed")
        try:
            recorded_command_timeout = float(command_timeout_raw)
        except (TypeError, ValueError) as exc:
            raise PromotionError("journal intent command timeout is malformed") from exc
        if recorded_command_timeout <= 0:
            raise PromotionError("journal intent command timeout must be positive")
        if prior_release is not None and (
            not isinstance(prior_release, str) or RELEASE_ID_RE.fullmatch(prior_release) is None
        ):
            raise PromotionError("journal intent prior release is malformed")
        for value, label in (
            (prior_manifest_sha256, "prior manifest digest"),
            (prior_previous_manifest_sha256, "prior previous manifest digest"),
            (expected_previous_manifest_sha256, "expected previous manifest digest"),
        ):
            if value is not None and (
                not isinstance(value, str)
                or re.fullmatch(r"[0-9a-f]{64}", value) is None
            ):
                raise PromotionError(f"journal {label} is malformed")
        for value, label in (
            (prior_previous_release, "prior previous release"),
            (expected_previous_release, "expected previous release"),
        ):
            if value is not None and (
                not isinstance(value, str) or RELEASE_ID_RE.fullmatch(value) is None
            ):
                raise PromotionError(f"journal {label} is malformed")
        if prior_audited_at is not None and not isinstance(prior_audited_at, str):
            raise PromotionError("journal prior audited_at is malformed")
        if prior_release is not None and prior_release not in {
            candidate.release_id,
            retained.release_id if retained is not None else None,
        }:
            raise PromotionError("journal prior release lacks a verified frozen artifact")
        if prior_release is None:
            verified_prior_manifest_sha256 = None
        elif (
            prior_manifest_sha256 in {None, candidate.manifest_sha256}
            and prior_release == candidate.release_id
        ):
            verified_prior_manifest_sha256 = candidate.manifest_sha256
        elif (
            retained is not None
            and prior_manifest_sha256 in {None, retained.manifest_sha256}
            and prior_release == retained.release_id
        ):
            verified_prior_manifest_sha256 = retained.manifest_sha256
        else:
            raise PromotionError(
                "journal prior manifest lacks a verified frozen artifact"
            )

        event_kinds = [event.get("kind") for event in journal.events]
        deploy_invocation_started = any(
            kind in {"deploy_invocation", "deploy_result"} for kind in event_kinds
        )
        rollback_pending = any(
            kind in {"rollback_intent", "rollback_invocation", "rollback_result"}
            for kind in event_kinds
        )

        first = self._stable_reconciliation_observation(reconcile_config)
        self._record_reconciliation_observation(
            journal, first, phase="explicit_reconciliation"
        )
        observed_release = first.selector.current_release_id if first.selector else None
        annotated_for_attempt = (
            first.annotations.get("workers/tag") == tag
            and first.annotations.get("workers/message") == message
        )
        candidate_selector_exact = (
            first.trust_source == expected_trust_source
            and first.selector_probe.status == 200
            and first.selector_probe.sha256 == expected_selector_sha256
            and first.selector is not None
            and first.selector.current_release_id == requested_release
            and first.selector.current_manifest_sha256 == expected_manifest_sha256
            and first.selector.previous_release_id == expected_previous_release
            and first.selector.previous_manifest_sha256
            == expected_previous_manifest_sha256
            and first.selector.audited_at == expected_audited_at
        )
        if annotated_for_attempt and not candidate_selector_exact:
            raise PromotionError(
                "attempt-owned Worker is live with selector bytes that differ from durable intent"
        )
        candidate_owned = annotated_for_attempt and candidate_selector_exact
        if candidate_owned:
            if rollback_pending:
                raise PromotionError(
                    "rollback was requested but the candidate is still live; complete or explicitly abandon the rollback before closing the attempt"
                )
            canary_ok, payload, _ = self._run_canary(
                requested_release,
                candidate.manifest_sha256,
                journal,
                "reconcile-candidate",
                expected_trust_source,
                verified_baseline,
            )
            append_event(journal, "canary_result", {"phase": "reconciliation", **payload})
            if not canary_ok:
                raise PromotionError("attempt-correlated candidate is live but its canary failed")
            post_canary = self._stable_reconciliation_observation(reconcile_config)
            self._record_reconciliation_observation(
                journal, post_canary, phase="reconciliation_post_canary"
            )
            if not self._same_reconciliation_state(first, post_canary):
                raise PromotionError("live state changed after the reconciliation canary")
            append_event(journal,
                "final_state",
                {
                    "outcome": "deployed_reconciled",
                    "release_id": requested_release,
                    "deployment_id": first.deployment.deployment_id,
                    "version_id": first.deployment.version_id,
                    "approval_note": self.approval_note,
                },
            )
            outcome = "deployed_reconciled"
        else:
            prior_selector_matches = (
                (
                    prior_selector_status == 404
                    and first.selector_probe.status == 404
                    and first.selector_probe.sha256 == prior_selector_sha256
                    and prior_release is None
                )
                or (
                    prior_selector_status == 200
                    and first.selector_probe.status == 200
                    and first.selector_probe.sha256 == prior_selector_sha256
                    and first.selector is not None
                    and observed_release == prior_release
                    and first.selector.previous_release_id == prior_previous_release
                    and first.selector.audited_at == prior_audited_at
                )
            )
            exact_prior = (
                first.trust_source == expected_trust_source
                and first.deployment.version_id == prior_version
                and prior_selector_matches
            )
            if not exact_prior:
                if not self.accept_external_supersession:
                    raise PromotionError(
                        "live state is neither the attempt-correlated candidate nor the exact prior state"
                    )
                if annotated_for_attempt:
                    raise PromotionError(
                        "attempt-owned inconsistent state cannot be accepted as external supersession"
                    )
                if rollback_pending:
                    raise PromotionError(
                        "rollback remains unresolved; external supersession cannot close this attempt"
                    )
                superseded, attempt_history = self._quiet_fence_uncertain_invocation(
                    journal=journal,
                    first=first,
                    tag=tag,
                    message=message,
                    config=reconcile_config,
                    recorded_command_timeout=recorded_command_timeout,
                    deploy_invocation_started=deploy_invocation_started,
                    phase_prefix="external_supersession",
                    approval=self.external_supersession_approval,
                    forbid_attempt_deployments=False,
                )
                append_event(
                    journal,
                    "final_state",
                    {
                        "outcome": "externally_superseded",
                        "release_id": observed_release,
                        "deployment_id": first.deployment.deployment_id,
                        "version_id": first.deployment.version_id,
                        "approval_note": self.approval_note,
                        "external_supersession_approval": self.external_supersession_approval,
                        "attempt_history_after_quiet": attempt_history,
                        "production_mutated_by_reconciliation": False,
                    },
                )
                outcome = "externally_superseded"
            else:
                same_predeploy_deployment = (
                    first.deployment.deployment_id == prior_deployment
                )
                if rollback_pending and same_predeploy_deployment:
                    raise PromotionError(
                        "rollback was requested but no distinct restored deployment is visible"
                    )
                if deploy_invocation_started and same_predeploy_deployment:
                    if not self.confirm_no_production_change:
                        raise PromotionError(
                            "deployment invocation was recorded; exact-prior closure requires "
                            "--confirm-no-production-change and a dedicated approval"
                        )
                quiet, attempt_history = self._quiet_fence_uncertain_invocation(
                    journal=journal,
                    first=first,
                    tag=tag,
                    message=message,
                    config=reconcile_config,
                    recorded_command_timeout=recorded_command_timeout,
                    deploy_invocation_started=deploy_invocation_started,
                    phase_prefix="prior_state",
                    approval=self.no_change_approval,
                    forbid_attempt_deployments=same_predeploy_deployment,
                )
                if prior_release is not None:
                    canary_ok, payload, _ = self._run_canary(
                        prior_release,
                        verified_prior_manifest_sha256,
                        journal,
                        "reconcile-prior",
                        expected_trust_source,
                    )
                    append_event(journal, "canary_result", {"phase": "reconciliation", **payload})
                    if not canary_ok:
                        raise PromotionError("prior release is live but its canary failed")
                post_canary = self._stable_reconciliation_observation(reconcile_config)
                self._record_reconciliation_observation(
                    journal, post_canary, phase="prior_state_post_canary"
                )
                if not self._same_reconciliation_state(quiet, post_canary):
                    raise PromotionError("prior live state changed after the reconciliation canary")
                outcome = (
                    "no_production_change"
                    if same_predeploy_deployment and deploy_invocation_started
                    else "aborted_before_deploy_reconciled"
                    if same_predeploy_deployment
                    else "rolled_back_unqualified_first_deploy"
                    if prior_release is None
                    else "rolled_back_reconciled"
                )
                append_event(journal,
                    "final_state",
                    {
                        "outcome": outcome,
                        "release_id": prior_release,
                        "deployment_id": first.deployment.deployment_id,
                        "version_id": first.deployment.version_id,
                        "approval_note": self.approval_note,
                        "quiet_seconds": self.reconcile_quiet_seconds,
                        "no_change_approval": (
                            self.no_change_approval
                            if outcome == "no_production_change"
                            else None
                        ),
                        "attempt_history_after_quiet": attempt_history,
                    },
                )

        print(
            json.dumps(
                {
                    "schema": RESULT_SCHEMA,
                    "ok": True,
                    "attempt_id": journal.attempt_id,
                    "outcome": outcome,
                    "journal_tip": journal.chain_tip,
                },
                sort_keys=True,
            )
        )
        return 0

    def run(self) -> int:
        self._validate_options()
        self.receipt_root = validate_target_receipt_root(
            self.receipt_root_input,
            self.repo.resolve(strict=True),
            self.base_url,
        )
        with PromotionLock(self.receipt_root):
            incomplete = list_incomplete_attempts(self.receipt_root)
            if self.mode == "reconcile":
                if len(incomplete) != 1 or incomplete[0].attempt_id != self.reconcile_attempt:
                    ids = [journal.attempt_id for journal in incomplete]
                    raise PromotionError(
                        f"reconciliation requires exactly the named incomplete attempt; found {ids}"
                    )
                return self.reconcile(incomplete[0])
            if incomplete:
                raise PromotionError(
                    "incomplete promotion attempt(s) block new work: "
                    + ", ".join(journal.attempt_id for journal in incomplete)
                )

            activation: ReviewedActivation | None = None
            if self.mode == "execute":
                activation = self._verify_reviewed_activation()
                prepared = self._prepare_reviewed_activation(activation)
            else:
                prepared = self.prepare()
            work_root = prepared.public_dir.parent
            try:
                if self.mode == "dry-run":
                    if self.retain_dry_run_store is not None:
                        assert self.receipt_root is not None
                        retained = retain_dry_run_artifacts(
                            prepared,
                            self.retain_dry_run_store,
                            repo_root=self.repo.resolve(strict=True),
                            receipt_root=self.receipt_root,
                        )
                        proposed_intent = self._retained_intent_payload(
                            prepared, retained
                        )
                    else:
                        proposed_intent = self._intent_payload(prepared)
                    print(
                        json.dumps(
                            {
                                "schema": DRY_RUN_SCHEMA,
                                "ok": True,
                                "attempt_id": prepared.attempt_id,
                                "proposed_intent": proposed_intent,
                                "production_mutated": False,
                            },
                            sort_keys=True,
                        )
                    )
                    return 0
                journal = EventJournal.create_with_intent(
                    self.receipt_root,
                    prepared.attempt_id,
                    journal_safe(self._intent_payload(prepared)),
                )
                try:
                    return self._deploy(prepared, journal)
                except Exception as exc:
                    try:
                        append_event(journal,
                            "observation",
                            {
                                "phase": "internal_error",
                                "error_type": type(exc).__name__,
                                "error": str(exc),
                                "manual_intervention_required": True,
                            },
                        )
                    except Exception:
                        pass
                    raise
            finally:
                if activation is None:
                    remove_sealed_tree(work_root)


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise PromotionError(f"{name} must be an integer") from exc
    return value


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    try:
        value = float(raw)
    except ValueError as exc:
        raise PromotionError(f"{name} must be a number") from exc
    if not math.isfinite(value):
        raise PromotionError(f"{name} must be finite")
    return value


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("release_id", nargs="?")
    parser.add_argument("--release-root", type=Path)
    parser.add_argument("--public-baseline-id")
    parser.add_argument("--public-baseline-root", type=Path)
    parser.add_argument("--activation-bundle-id")
    parser.add_argument("--activation-bundle-root", type=Path)
    parser.add_argument("--expected-semantic-baseline-id")
    parser.add_argument("--repo-root", type=Path, required=True)
    parser.add_argument("--python", type=Path, required=True)
    parser.add_argument(
        "--receipt-dir",
        type=Path,
        default=Path(os.environ["WIKILEAN_BRAIN_RECEIPT_DIR"])
        if os.environ.get("WIKILEAN_BRAIN_RECEIPT_DIR")
        else None,
    )
    modes = parser.add_mutually_exclusive_group(required=True)
    modes.add_argument("--dry-run", action="store_true")
    modes.add_argument("--execute", action="store_true")
    modes.add_argument("--reconcile-attempt")
    parser.add_argument(
        "--retain-dry-run-store",
        type=Path,
        help=(
            "dry-run only: atomically retain sealed deploy inputs and raw preflight "
            "evidence in this absolute external content-addressed store"
        ),
    )
    parser.add_argument(
        "--allow-first-deploy-without-selector",
        action="store_true",
        help=(
            "admit an HTTP 404 selector as first-deploy prestate; provisional in "
            "--dry-run, and paired approval is required with --execute"
        ),
    )
    parser.add_argument(
        "--first-deploy-approval",
        help="execute-only approval for the reviewed first-deployment exception",
    )
    parser.add_argument("--approval-note")
    parser.add_argument(
        "--reconcile-quiet-seconds",
        type=float,
        default=_env_float("WIKILEAN_BRAIN_RECONCILE_QUIET_SECONDS", 900),
    )
    parser.add_argument("--confirm-no-production-change", action="store_true")
    parser.add_argument("--no-change-approval")
    parser.add_argument("--accept-external-supersession", action="store_true")
    parser.add_argument("--external-supersession-approval")
    parser.add_argument(
        "--canary-timeout",
        type=float,
        default=_env_float("WIKILEAN_BRAIN_CANARY_TIMEOUT", 300),
    )
    parser.add_argument(
        "--canary-interval",
        type=float,
        default=_env_float("WIKILEAN_BRAIN_CANARY_INTERVAL", 5),
    )
    parser.add_argument(
        "--canary-max-response-bytes",
        type=int,
        default=_env_int("WIKILEAN_BRAIN_CANARY_MAX_RESPONSE_BYTES", 32 * 1024 * 1024),
    )
    parser.add_argument(
        "--status-attempts",
        type=int,
        default=_env_int("WIKILEAN_BRAIN_STATUS_ATTEMPTS", 12),
    )
    parser.add_argument(
        "--status-interval",
        type=float,
        default=_env_float("WIKILEAN_BRAIN_STATUS_INTERVAL", 5),
    )
    parser.add_argument(
        "--command-timeout",
        type=float,
        default=_env_float("WIKILEAN_BRAIN_COMMAND_TIMEOUT", 900),
    )
    parser.add_argument("--attempt-id", help=argparse.SUPPRESS)
    parser.add_argument("--audited-at", help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    if args.receipt_dir is None:
        parser.error("--receipt-dir or WIKILEAN_BRAIN_RECEIPT_DIR is required")
    if args.reconcile_attempt:
        if any(
            value is not None
            for value in (
                args.release_id,
                args.release_root,
                args.public_baseline_id,
                args.public_baseline_root,
                args.activation_bundle_id,
                args.activation_bundle_root,
                args.expected_semantic_baseline_id,
            )
        ):
            parser.error(
                "reconciliation does not accept release, baseline, or activation-bundle arguments"
            )
    elif any(
        value is None
        for value in (
            args.release_id,
            args.release_root,
            args.public_baseline_id,
            args.public_baseline_root,
        )
    ):
        parser.error(
            "promotion requires release_id, --release-root, --public-baseline-id, "
            "and --public-baseline-root"
        )
    if args.retain_dry_run_store is not None and not args.dry_run:
        parser.error("--retain-dry-run-store is valid only with --dry-run")
    if args.execute and os.environ.get("WIKILEAN_BRAIN_DEPLOY") != "1":
        parser.error("--execute requires WIKILEAN_BRAIN_DEPLOY=1")
    return args


def main(argv: list[str] | None = None) -> int:
    try:
        args = parse_args(argv)
        mode = "reconcile" if args.reconcile_attempt else "execute" if args.execute else "dry-run"
        promoter = BrainPromoter(
            repo_root=args.repo_root,
            python=args.python,
            release_id=args.release_id,
            release_root=args.release_root,
            public_baseline_id=args.public_baseline_id,
            public_baseline_root=args.public_baseline_root,
            activation_bundle_id=args.activation_bundle_id,
            activation_bundle_root=args.activation_bundle_root,
            expected_semantic_baseline_id=args.expected_semantic_baseline_id,
            receipt_root=args.receipt_dir,
            base_url=PRODUCTION_ORIGIN,
            mode=mode,
            allow_first_deploy=args.allow_first_deploy_without_selector,
            first_deploy_approval=args.first_deploy_approval,
            approval_note=args.approval_note,
            reconcile_attempt=args.reconcile_attempt,
            reconcile_quiet_seconds=args.reconcile_quiet_seconds,
            confirm_no_production_change=args.confirm_no_production_change,
            no_change_approval=args.no_change_approval,
            accept_external_supersession=args.accept_external_supersession,
            external_supersession_approval=args.external_supersession_approval,
            canary_timeout=args.canary_timeout,
            canary_interval=args.canary_interval,
            canary_max_response_bytes=args.canary_max_response_bytes,
            status_attempts=args.status_attempts,
            status_interval=args.status_interval,
            command_timeout=args.command_timeout,
            attempt_id=args.attempt_id,
            audited_at=args.audited_at,
            retain_dry_run_store=args.retain_dry_run_store,
        )
        return promoter.run()
    except KeyboardInterrupt:
        print(
            json.dumps(
                {
                    "schema": RESULT_SCHEMA,
                    "ok": False,
                    "error": "interrupted; any durable attempt remains incomplete and must be reconciled",
                },
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return 130
    except (PromotionError, JournalError, TransportError, OSError, ValueError) as exc:
        print(
            json.dumps(
                {
                    "schema": RESULT_SCHEMA,
                    "ok": False,
                    "error": str(exc),
                },
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
