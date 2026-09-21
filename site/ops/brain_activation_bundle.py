#!/usr/bin/env python3
"""Freeze and verify a complete, non-mutating Brain activation review bundle.

The bundle is deliberately evidence-only.  It binds the candidate and semantic
baseline release manifests, one verified public-asset baseline, the committed
source attestation, shadow results, a complete seven-artifact semantic
comparison, the promoter dry-run, and the two-worktree build context.  It never
invokes a build or production mutation.
"""
from __future__ import annotations

import argparse
import contextlib
import decimal
import fcntl
import hashlib
import json
import os
import re
import stat
import subprocess
import sys
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence


HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parents[1]
BRAIN_TOOLS = REPO_ROOT / "brain" / "tools"
if str(BRAIN_TOOLS) not in sys.path:
    sys.path.insert(0, str(BRAIN_TOOLS))

from authority_contracts import (  # noqa: E402
    BRAIN_SQLITE_APPLICATION_ID,
    COMPATIBILITY_SEMANTIC_PATHS,
    VerificationError,
    canonical_artifact_json_bytes,
    canonical_json_bytes as release_canonical_json_bytes,
    load_canonical_json,
    parse_artifact_json_bytes,
    validate_release_manifest,
    validate_release_selector,
    verify_release_files,
)
from brain_public_baseline import (  # noqa: E402
    BaselineValidationError,
    PublicAssetBaseline,
    PublicAssetFile,
    SOURCE_ATTESTATION_PATH,
    validate_public_baseline_manifest,
    verify_public_baseline,
)
import semantic_diff as semantic_diff_tool  # noqa: E402
import measure_store as measure_store_tool  # noqa: E402
from brain_activation_ci import (  # noqa: E402
    ActivationCIError,
    EVIDENCE_SCHEMA as CI_EVIDENCE_SCHEMA,
    record_activation_ci,
    validate_ci_evidence,
)
from brain_promote_release import (  # noqa: E402
    DRY_RUN_ARTIFACT_SCHEMA,
    PromotionError,
    extract_last_json_value,
    parse_deployment_status,
    verify_retained_dry_run_artifacts,
)


BUNDLE_SCHEMA = "wikilean.brain-activation-bundle/v1"
BUNDLE_DOMAIN = "wikilean.brain-activation-bundle.v1"
BUILD_CONTEXT_SCHEMA = "wikilean.brain-activation-build-context/v1"
SEMANTIC_DIFF_SCHEMA = "wikilean.semantic-diff/v2"
RELEASE_SCHEMA = "wikilean.release/v1"
BASELINE_SCHEMA = "wikilean.public-asset-baseline/v1"
SOURCE_ATTESTATION_SCHEMA = "wikilean.public-asset-source-attestation/v1"
METRICS_SCHEMA = "wikilean.brain.store-metrics.v1"
PUBLIC_RESULT_SCHEMA = "wikilean.public-build-result/v1"
PUBLIC_STAGE_SCHEMA = "wikilean.public-stage-result/v1"
DRY_RUN_SCHEMA = "wikilean.brain-promotion-dry-run/v1"
MANIFEST_NAME = "manifest.json"
MAX_JSON_BYTES = 64 * 1024 * 1024
COPY_BUFFER_BYTES = 1024 * 1024
ACTIVATION_METRICS_LIMIT = 100
ACTIVATION_METRICS_ITERATIONS = 5
ACTIVATION_METRICS_WARMUP = 1
ACTIVATION_METRICS_CHECK_LIMIT = 100
PUBLIC_BRAIN_PREFIX = "site/assets/brain/"
PUBLIC_CELLS_PREFIX = PUBLIC_BRAIN_PREFIX + "cells/"
PUBLIC_RELEASE_MANIFEST = "release.json"

RELEASE_ID_RE = re.compile(r"^sha256:([0-9a-f]{64})$")
DIGEST_RE = re.compile(r"^[0-9a-f]{64}$")
HASH_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
GIT_COMMIT_RE = re.compile(r"^[0-9a-f]{40}$")
UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
)
PRODUCTION_ORIGIN = "https://wikilean.jackmccarthy.org"

EVIDENCE_PATHS: tuple[tuple[str, str], ...] = (
    ("candidate_release_manifest", "candidate-release.json"),
    ("semantic_baseline_manifest", "semantic-baseline-release.json"),
    ("public_baseline_manifest", "public-baseline.json"),
    ("source_attestation", "public-asset-source-attestation.json"),
    ("release_result", "release-result.json"),
    ("release_metrics", "release-metrics.json"),
    ("shadow_public_result", "shadow-public-result.json"),
    ("semantic_diff", "semantic-diff.json"),
    ("promoter_dry_run", "promoter-dry-run.json"),
    ("build_context", "build-context.json"),
    ("ci_evidence", "ci-evidence.json"),
)
EXTERNAL_EVIDENCE_PATHS = tuple(
    item for item in EVIDENCE_PATHS if item[0] != "ci_evidence"
)
EVIDENCE_BY_KIND = dict(EVIDENCE_PATHS)
EVIDENCE_BY_PATH = {path: kind for kind, path in EVIDENCE_PATHS}


class ActivationBundleError(RuntimeError):
    """Base class for activation-bundle failures."""


class BundleValidationError(ActivationBundleError):
    """Evidence or a frozen bundle violates the activation contract."""


class BundleFreezeError(ActivationBundleError):
    """A bundle cannot be published safely or durably."""


@dataclass(frozen=True)
class EvidenceFile:
    kind: str
    path: str
    sha256: str
    bytes: int


@dataclass(frozen=True)
class ActivationBundle:
    root: Path
    manifest_path: Path
    bundle_id: str
    bundle_hex: str
    release_id: str
    semantic_baseline_release_id: str
    baseline_id: str
    authority_git_commit: str
    reducer_git_commit: str
    files: tuple[EvidenceFile, ...]

    def summary(self) -> dict[str, object]:
        return {
            "ok": True,
            "schema": BUNDLE_SCHEMA,
            "bundle_id": self.bundle_id,
            "root": str(self.root),
            "manifest": str(self.manifest_path),
            "release_id": self.release_id,
            "semantic_baseline_release_id": self.semantic_baseline_release_id,
            "baseline_id": self.baseline_id,
            "authority_git_commit": self.authority_git_commit,
            "reducer_git_commit": self.reducer_git_commit,
            "files": len(self.files),
            "bytes": sum(item.bytes for item in self.files),
        }


@dataclass(frozen=True)
class ValidatedInputs:
    release_id: str
    release_hex: str
    semantic_baseline_release_id: str
    baseline_id: str
    authority_commit: str
    reducer_commit: str
    release_root: Path
    semantic_baseline_root: Path
    baseline_root: Path
    build_root: Path
    promotion_root: Path
    git: Path | None
    baseline_files: tuple[PublicAssetFile, ...]
    documents: Mapping[str, Mapping[str, Any]]
    canonical_bytes: Mapping[str, bytes]


def _canonical_json_bytes(value: object) -> bytes:
    try:
        return canonical_artifact_json_bytes(value) + b"\n"
    except VerificationError as exc:
        raise BundleValidationError(f"cannot encode canonical JSON: {exc}") from exc


def _parse_json_bytes(raw: bytes, label: str, *, require_canonical: bool) -> dict[str, Any]:
    if len(raw) > MAX_JSON_BYTES:
        raise BundleValidationError(f"{label} exceeds the supported size limit")
    try:
        value = parse_artifact_json_bytes(raw, location=label)
    except VerificationError as exc:
        raise BundleValidationError(str(exc)) from exc
    if not isinstance(value, dict):
        raise BundleValidationError(f"{label} must be a JSON object")
    canonical = _canonical_json_bytes(value)
    if require_canonical and canonical != raw:
        raise BundleValidationError(f"{label} is not canonical JSON")
    return value


def _signature(value: os.stat_result) -> tuple[int, ...]:
    return (
        value.st_dev,
        value.st_ino,
        value.st_mode,
        value.st_nlink,
        value.st_size,
        value.st_mtime_ns,
        value.st_ctime_ns,
    )


def _read_source_json(path: Path, label: str) -> tuple[dict[str, Any], bytes]:
    if not path.is_absolute():
        raise BundleValidationError(f"{label} path must be absolute")
    if path.is_symlink():
        raise BundleValidationError(f"{label} must not be a symlink")
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise BundleValidationError(f"cannot open {label}: {exc}") from exc
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode):
            raise BundleValidationError(f"{label} must be a regular file")
        if before.st_nlink != 1:
            raise BundleValidationError(f"{label} must not be hard-linked")
        if before.st_size > MAX_JSON_BYTES:
            raise BundleValidationError(f"{label} exceeds the supported size limit")
        chunks: list[bytes] = []
        total = 0
        while True:
            chunk = os.read(descriptor, COPY_BUFFER_BYTES)
            if not chunk:
                break
            total += len(chunk)
            if total > MAX_JSON_BYTES:
                raise BundleValidationError(f"{label} exceeds the supported size limit")
            chunks.append(chunk)
        after = os.fstat(descriptor)
    finally:
        os.close(descriptor)
    if _signature(before) != _signature(after) or total != before.st_size:
        raise BundleValidationError(f"{label} changed while reading")
    document = _parse_json_bytes(b"".join(chunks), label, require_canonical=False)
    return document, _canonical_json_bytes(document)


def _exact_keys(
    value: Mapping[str, object], expected: set[str] | frozenset[str], label: str
) -> None:
    actual = set(value)
    missing = sorted(expected - actual)
    unknown = sorted(actual - expected)
    if missing or unknown:
        details: list[str] = []
        if missing:
            details.append("missing " + ", ".join(missing))
        if unknown:
            details.append("unknown " + ", ".join(unknown))
        raise BundleValidationError(f"{label} fields are invalid ({'; '.join(details)})")


def _require_object(value: object, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise BundleValidationError(f"{label} must be a JSON object")
    return value


def _require_string(value: object, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise BundleValidationError(f"{label} must be a non-empty string")
    return value


def _require_release_id(value: object, label: str) -> tuple[str, str]:
    text = _require_string(value, label)
    match = RELEASE_ID_RE.fullmatch(text)
    if match is None:
        raise BundleValidationError(f"{label} must be sha256:<64 lowercase hex digits>")
    return text, match.group(1)


def _require_commit(value: object, label: str) -> str:
    text = _require_string(value, label)
    if GIT_COMMIT_RE.fullmatch(text) is None:
        raise BundleValidationError(f"{label} must be a full lowercase Git commit")
    return text


def _require_digest(value: object, label: str, *, prefixed: bool = False) -> str:
    text = _require_string(value, label)
    pattern = HASH_RE if prefixed else DIGEST_RE
    if pattern.fullmatch(text) is None:
        prefix = "sha256:" if prefixed else ""
        raise BundleValidationError(f"{label} must be {prefix}<64 lowercase hex digits>")
    return text


def _require_bool(value: object, label: str) -> bool:
    if not isinstance(value, bool):
        raise BundleValidationError(f"{label} must be a boolean")
    return value


def _require_int(value: object, label: str, *, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise BundleValidationError(f"{label} must be an integer >= {minimum}")
    return value


def _require_number(value: object, label: str, *, minimum: int = 0) -> int | decimal.Decimal:
    if isinstance(value, bool) or not isinstance(value, (int, decimal.Decimal)):
        raise BundleValidationError(f"{label} must be a finite number")
    if value < minimum:
        raise BundleValidationError(f"{label} must be >= {minimum}")
    return value


def _validate_tree_inventory(value: object, label: str) -> dict[str, Any]:
    tree = _require_object(value, label)
    _exact_keys(tree, {"schema", "root", "objects", "bytes", "sha256"}, label)
    if tree.get("schema") != "wikilean.file-tree-inventory/v1":
        raise BundleValidationError(f"{label} schema mismatch")
    _declared_absolute_path(tree.get("root"), f"{label}.root")
    _require_int(tree.get("objects"), f"{label}.objects", minimum=1)
    _require_int(tree.get("bytes"), f"{label}.bytes", minimum=1)
    _require_digest(tree.get("sha256"), f"{label}.sha256")
    return tree


def _physical_absolute_path(value: object, label: str, *, expect_dir: bool) -> Path:
    text = _require_string(value, label)
    path = Path(text)
    if not path.is_absolute():
        raise BundleValidationError(f"{label} must be absolute")
    if path.is_symlink():
        raise BundleValidationError(f"{label} must not be a symlink")
    try:
        resolved = path.resolve(strict=True)
    except OSError as exc:
        raise BundleValidationError(f"{label} does not resolve: {exc}") from exc
    if str(resolved) != text:
        raise BundleValidationError(f"{label} must be its physical canonical path")
    if expect_dir and not resolved.is_dir():
        raise BundleValidationError(f"{label} must be a directory")
    if not expect_dir and not resolved.is_file():
        raise BundleValidationError(f"{label} must be a regular file")
    return resolved


def _declared_absolute_path(value: object, label: str) -> Path:
    text = _require_string(value, label)
    path = Path(text)
    if not path.is_absolute() or os.path.normpath(text) != text:
        raise BundleValidationError(f"{label} must be an absolute normalized path")
    return path


def _is_within(root: Path, candidate: Path) -> bool:
    try:
        candidate.relative_to(root)
        return True
    except ValueError:
        return False


def _overlap(first: Path, second: Path) -> bool:
    return _is_within(first, second) or _is_within(second, first)


def _inventory_tree(root: Path) -> dict[str, object]:
    """Recompute the promoter's byte-level tree inventory."""
    resolved = root.resolve(strict=True)
    if root.is_symlink() or resolved != root.absolute():
        raise BundleValidationError(f"tree root must not use symlink aliases: {root}")
    digest = hashlib.sha256()
    digest.update(b"wikilean\0wikilean.file-tree.v1\0")
    objects = 0
    byte_count = 0
    for path in sorted(
        resolved.rglob("*"), key=lambda value: value.relative_to(resolved).as_posix()
    ):
        relative = path.relative_to(resolved).as_posix()
        if path.is_symlink():
            raise BundleValidationError(f"tree contains a symlink: {relative}")
        if path.is_dir():
            continue
        if not path.is_file():
            raise BundleValidationError(f"tree contains a non-regular entry: {relative}")
        body = path.read_bytes()
        encoded = relative.encode("utf-8")
        digest.update(len(encoded).to_bytes(4, "big"))
        digest.update(encoded)
        digest.update(len(body).to_bytes(8, "big"))
        digest.update(hashlib.sha256(body).digest())
        objects += 1
        byte_count += len(body)
    return {
        "schema": "wikilean.file-tree-inventory/v1",
        "root": str(resolved),
        "objects": objects,
        "bytes": byte_count,
        "sha256": digest.hexdigest(),
    }


def _public_path_for_release_artifact(relative: str) -> str | None:
    if relative == PUBLIC_BRAIN_PREFIX + "sources.json":
        return "sources.json"
    if relative == PUBLIC_BRAIN_PREFIX + "xref_index.json":
        return "xref_index.json"
    if relative.startswith(PUBLIC_CELLS_PREFIX):
        return "cells/" + relative.removeprefix(PUBLIC_CELLS_PREFIX)
    return None


def _read_physical_file(path: Path, label: str) -> bytes:
    physical = _physical_absolute_path(str(path), label, expect_dir=False)
    try:
        return physical.read_bytes()
    except OSError as exc:
        raise BundleValidationError(f"cannot read {label}: {exc}") from exc


def _scan_regular_tree(root: Path, label: str) -> dict[str, tuple[int, str]]:
    physical = _physical_absolute_path(str(root), label, expect_dir=True)
    result: dict[str, tuple[int, str]] = {}
    for path in sorted(
        physical.rglob("*"), key=lambda value: value.relative_to(physical).as_posix()
    ):
        relative = path.relative_to(physical).as_posix()
        info = path.lstat()
        if stat.S_ISLNK(info.st_mode):
            raise BundleValidationError(f"{label} contains a symlink: {relative}")
        if stat.S_ISDIR(info.st_mode):
            continue
        if not stat.S_ISREG(info.st_mode):
            raise BundleValidationError(f"{label} contains a non-regular entry: {relative}")
        raw = path.read_bytes()
        result[relative] = (len(raw), hashlib.sha256(raw).hexdigest())
    return result


def _git_environment() -> dict[str, str]:
    blocked = {
        "GIT_DIR",
        "GIT_WORK_TREE",
        "GIT_INDEX_FILE",
        "GIT_OBJECT_DIRECTORY",
        "GIT_ALTERNATE_OBJECT_DIRECTORIES",
        "GIT_COMMON_DIR",
        "GIT_NAMESPACE",
        "GIT_REPLACE_REF_BASE",
    }
    environment = {
        key: value
        for key, value in os.environ.items()
        if key not in blocked and not key.startswith("GIT_CONFIG_")
    }
    environment.update(
        {
            "GIT_NO_REPLACE_OBJECTS": "1",
            "GIT_OPTIONAL_LOCKS": "0",
            "LC_ALL": "C",
            "PATH": os.defpath,
        }
    )
    return environment


def _approved_git_path(raw: Path) -> Path:
    candidate = raw.expanduser()
    if not candidate.is_absolute():
        raise BundleValidationError("approved Git executable path must be absolute")
    path = Path(os.path.abspath(candidate))
    try:
        target = path.resolve(strict=True) if path.is_symlink() else path
    except OSError as exc:
        raise BundleValidationError(f"approved Git executable is invalid: {exc}") from exc
    if not target.is_file() or not os.access(path, os.X_OK):
        raise BundleValidationError("approved Git executable must be an executable regular file")
    return path


def _git(git: Path, root: Path, arguments: Sequence[str], label: str) -> bytes:
    try:
        result = subprocess.run(
            [str(git), "-C", str(root), *arguments],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
            timeout=30,
            env=_git_environment(),
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise BundleValidationError(f"cannot inspect {label}: {exc}") from exc
    if result.returncode != 0:
        detail = result.stderr.decode("utf-8", errors="replace").strip()
        raise BundleValidationError(f"cannot inspect {label}: {detail or 'git failed'}")
    return result.stdout


def _inspect_worktree(
    raw: object, label: str, *, git: Path
) -> tuple[Path, str, str, bool]:
    value = _require_object(raw, label)
    _exact_keys(value, {"root", "head", "branch", "clean"}, label)
    root = _physical_absolute_path(value["root"], f"{label}.root", expect_dir=True)
    declared_head = _require_commit(value["head"], f"{label}.head")
    declared_branch = _require_string(value["branch"], f"{label}.branch")
    declared_clean = _require_bool(value["clean"], f"{label}.clean")

    top = Path(
        _git(git, root, ["rev-parse", "--show-toplevel"], f"{label} top-level")
        .decode("utf-8", errors="strict")
        .strip()
    ).resolve(strict=True)
    if top != root:
        raise BundleValidationError(f"{label}.root is not the Git worktree top-level")
    actual_head = (
        _git(git, root, ["rev-parse", "HEAD"], f"{label} HEAD")
        .decode("ascii", errors="strict")
        .strip()
    )
    if actual_head != declared_head:
        raise BundleValidationError(
            f"{label}.head disagrees with Git: expected {declared_head}, found {actual_head}"
        )
    try:
        branch_result = subprocess.run(
            [str(git), "-C", str(root), "symbolic-ref", "--quiet", "--short", "HEAD"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
            timeout=30,
            env=_git_environment(),
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise BundleValidationError(f"cannot inspect {label} branch: {exc}") from exc
    if branch_result.returncode == 0:
        actual_branch = branch_result.stdout.decode("utf-8", errors="strict").strip()
    elif branch_result.returncode == 1:
        actual_branch = "detached"
    else:
        raise BundleValidationError(f"cannot inspect {label} branch")
    if actual_branch != declared_branch:
        raise BundleValidationError(
            f"{label}.branch disagrees with Git: expected {declared_branch!r}, "
            f"found {actual_branch!r}"
        )
    porcelain = _git(
        git,
        root,
        ["status", "--porcelain=v1", "--untracked-files=all"],
        f"{label} status",
    )
    actual_clean = not porcelain.strip()
    if actual_clean != declared_clean:
        raise BundleValidationError(
            f"{label}.clean disagrees with Git: expected {declared_clean}, found {actual_clean}"
        )
    return root, actual_head, actual_branch, actual_clean


def _validate_build_context(
    document: Mapping[str, Any],
    authority_commit: str,
    *,
    inspect_external: bool,
    git: Path | None = None,
) -> tuple[Path, Path]:
    _exact_keys(
        document,
        {"schema", "authority_git_commit", "build_worktree", "promotion_worktree"},
        "build context",
    )
    if document.get("schema") != BUILD_CONTEXT_SCHEMA:
        raise BundleValidationError("build context schema mismatch")
    if _require_commit(document.get("authority_git_commit"), "build context authority") != authority_commit:
        raise BundleValidationError("build context authority differs from the candidate release")
    build_value = _require_object(document.get("build_worktree"), "build context build_worktree")
    promotion_value = _require_object(
        document.get("promotion_worktree"), "build context promotion_worktree"
    )
    for value, label in (
        (build_value, "build context build_worktree"),
        (promotion_value, "build context promotion_worktree"),
    ):
        _exact_keys(value, {"root", "head", "branch", "clean"}, label)
    if inspect_external:
        if git is None:
            raise BundleValidationError(
                "approved Git executable is required for external worktree inspection"
            )
        build_root, build_head, _, _ = _inspect_worktree(
            build_value, "build context build_worktree", git=git
        )
        promotion_root, promotion_head, promotion_branch, promotion_clean = _inspect_worktree(
            promotion_value, "build context promotion_worktree", git=git
        )
    else:
        build_root = _declared_absolute_path(
            build_value.get("root"), "build context build_worktree.root"
        )
        promotion_root = _declared_absolute_path(
            promotion_value.get("root"), "build context promotion_worktree.root"
        )
        build_head = _require_commit(
            build_value.get("head"), "build context build_worktree.head"
        )
        promotion_head = _require_commit(
            promotion_value.get("head"), "build context promotion_worktree.head"
        )
        _require_string(build_value.get("branch"), "build context build_worktree.branch")
        _require_bool(build_value.get("clean"), "build context build_worktree.clean")
        promotion_branch = _require_string(
            promotion_value.get("branch"), "build context promotion_worktree.branch"
        )
        promotion_clean = _require_bool(
            promotion_value.get("clean"), "build context promotion_worktree.clean"
        )
    if build_head != authority_commit or promotion_head != authority_commit:
        raise BundleValidationError("both worktrees must be at the candidate authority commit")
    if build_root == promotion_root or _overlap(build_root, promotion_root):
        raise BundleValidationError("build and promotion worktrees must be distinct and non-overlapping")
    if promotion_branch not in {"main", "detached"}:
        raise BundleValidationError("promotion worktree must be on main or detached HEAD")
    if not promotion_clean:
        raise BundleValidationError("promotion worktree must be clean")
    if inspect_external:
        executing_root = REPO_ROOT.resolve(strict=True)
        if promotion_root != executing_root:
            raise BundleValidationError(
                "the bundle tool must run from the declared promotion worktree"
            )
        main_commit = (
            _git(
                git,
                promotion_root,
                ["rev-parse", "refs/heads/main"],
                "promotion worktree main ref",
            )
            .decode("ascii", errors="strict")
            .strip()
        )
        if main_commit != authority_commit:
            raise BundleValidationError(
                "refs/heads/main must equal the candidate authority commit"
            )
        git_dir = Path(
            _git(
                git,
                promotion_root,
                ["rev-parse", "--absolute-git-dir"],
                "promotion worktree Git directory",
            )
            .decode("utf-8", errors="strict")
            .strip()
        )
        if any(
            path.exists()
            for path in (
                git_dir / "MERGE_HEAD",
                git_dir / "rebase-merge",
                git_dir / "rebase-apply",
            )
        ):
            raise BundleValidationError(
                "promotion worktree has a merge or rebase in progress"
            )
    return build_root, promotion_root


def _validate_public_result(
    document: Mapping[str, Any],
    *,
    release_id: str,
    release_hex: str,
    baseline_id: str,
    baseline_authority: str,
    baseline_root: Path,
    baseline_files: int,
    baseline_bytes: int,
    require_baseline: bool,
    label: str,
) -> None:
    _exact_keys(
        document,
        {
            "schema",
            "public_dir",
            "mathlib_declarations",
            "public_baseline",
            "duration_ms",
            "max_rss_bytes",
            "brain",
        },
        label,
    )
    if document.get("schema") != PUBLIC_RESULT_SCHEMA:
        raise BundleValidationError(f"{label} schema mismatch")
    public_dir = _declared_absolute_path(document.get("public_dir"), f"{label}.public_dir")
    _require_int(
        document.get("mathlib_declarations"),
        f"{label}.mathlib_declarations",
        minimum=1,
    )
    _require_number(document.get("duration_ms"), f"{label}.duration_ms")
    _require_int(document.get("max_rss_bytes"), f"{label}.max_rss_bytes", minimum=1)
    raw_baseline = document.get("public_baseline")
    if require_baseline:
        baseline_result = _require_object(raw_baseline, f"{label}.public_baseline")
        _exact_keys(
            baseline_result,
            {"schema", "baseline_id", "authority_commit", "root", "files", "bytes"},
            f"{label}.public_baseline",
        )
        if (
            baseline_result.get("schema") != BASELINE_SCHEMA
            or baseline_result.get("baseline_id") != baseline_id
            or baseline_result.get("authority_commit") != baseline_authority
            or baseline_result.get("root") != str(baseline_root)
            or baseline_result.get("files") != baseline_files
            or baseline_result.get("bytes") != baseline_bytes
        ):
            raise BundleValidationError(f"{label} names the wrong public baseline")
    elif raw_baseline is not None:
        raise BundleValidationError(
            f"{label}.public_baseline must be null for legacy shadow staging"
        )
    brain = _require_object(document.get("brain"), f"{label}.brain")
    _exact_keys(
        brain,
        {
            "schema",
            "release_id",
            "release",
            "previous_release_id",
            "retained_release_ids",
            "destination",
            "objects",
            "bytes",
            "largest_file_bytes",
            "copy_buffer_bytes",
            "duration_ms",
            "max_rss_bytes",
            "free_bytes_before",
            "free_bytes_after",
            "brain_page",
            "warnings",
        },
        f"{label}.brain",
    )
    if (
        brain.get("schema") != PUBLIC_STAGE_SCHEMA
        or brain.get("release_id") != release_id
        or brain.get("release") != release_hex
        or brain.get("warnings") != []
    ):
        raise BundleValidationError(f"{label} names the wrong or unhealthy Brain release")
    retained = brain.get("retained_release_ids")
    if (
        not isinstance(retained, list)
        or not retained
        or retained[0] != release_id
        or len(retained) != len(set(retained))
        or any(RELEASE_ID_RE.fullmatch(item) is None for item in retained if isinstance(item, str))
        or not all(isinstance(item, str) for item in retained)
    ):
        raise BundleValidationError(f"{label} does not retain the candidate as current")
    previous = brain.get("previous_release_id")
    if previous is not None:
        _require_release_id(previous, f"{label}.brain.previous_release_id")
        if len(retained) < 2 or retained[1] != previous:
            raise BundleValidationError(f"{label} previous release is not retained second")
    elif len(retained) != 1:
        raise BundleValidationError(f"{label} retained unexpected releases without previous")
    destination = _declared_absolute_path(
        brain.get("destination"), f"{label}.brain.destination"
    )
    if destination != public_dir / "assets" / "brain":
        raise BundleValidationError(f"{label}.brain.destination is inconsistent")
    for field, minimum in (
        ("objects", 1),
        ("bytes", 1),
        ("largest_file_bytes", 1),
        ("copy_buffer_bytes", 1),
        ("max_rss_bytes", 1),
        ("free_bytes_before", 0),
        ("free_bytes_after", 0),
    ):
        _require_int(brain.get(field), f"{label}.brain.{field}", minimum=minimum)
    _require_number(brain.get("duration_ms"), f"{label}.brain.duration_ms")
    page = brain.get("brain_page")
    if not isinstance(page, dict):
        raise BundleValidationError(f"{label} omitted the verified frozen Brain page")
    _exact_keys(page, {"destination", "bytes", "sha256"}, f"{label}.brain.brain_page")
    page_destination = _declared_absolute_path(
        page.get("destination"), f"{label}.brain.brain_page.destination"
    )
    if page_destination != public_dir / "brain.html":
        raise BundleValidationError(f"{label}.brain.brain_page.destination is inconsistent")
    _require_int(page.get("bytes"), f"{label}.brain.brain_page.bytes", minimum=1)
    _require_digest(page.get("sha256"), f"{label}.brain.brain_page.sha256")


def _validate_release_result(
    document: Mapping[str, Any],
    *,
    release_id: str,
    release_hex: str,
    artifact_count: int,
    byte_count: int,
) -> None:
    _exact_keys(
        document,
        {
            "artifact_count",
            "byte_count",
            "manifest",
            "release",
            "release_id",
            "reused",
            "root",
        },
        "release result",
    )
    if document.get("release_id") != release_id or document.get("release") != release_hex:
        raise BundleValidationError("release result names the wrong release")
    if document.get("artifact_count") != artifact_count or document.get("byte_count") != byte_count:
        raise BundleValidationError("release result counts differ from the release manifest")
    _require_bool(document.get("reused"), "release result reused")


def _validate_shadow_public_output(
    document: Mapping[str, Any],
    *,
    expected_public: Path,
    release_root: Path | None,
    release_manifest: Mapping[str, Any],
    release_manifest_bytes: bytes,
    release_id: str,
    release_hex: str,
) -> None:
    """Verify the shadow tree itself, not only the builder's JSON claim."""
    public_dir = _physical_absolute_path(
        document.get("public_dir"), "shadow public result.public_dir", expect_dir=True
    )
    if public_dir != expected_public:
        raise BundleValidationError("staged public output differs from its expected root")
    brain = _require_object(document.get("brain"), "shadow public result.brain")
    destination = _physical_absolute_path(
        brain.get("destination"), "shadow public result.brain.destination", expect_dir=True
    )
    if destination != public_dir / "assets" / "brain":
        raise BundleValidationError("shadow Brain destination differs from public_dir/assets/brain")
    page = _require_object(brain.get("brain_page"), "shadow public result.brain.brain_page")
    page_path = _physical_absolute_path(
        page.get("destination"),
        "shadow public result.brain.brain_page.destination",
        expect_dir=False,
    )
    if page_path != public_dir / "brain.html":
        raise BundleValidationError("shadow Brain page destination differs from public_dir/brain.html")

    artifacts = {
        item.get("path"): item
        for item in release_manifest.get("artifacts", [])
        if isinstance(item, dict)
    }
    page_ref = artifacts.get("site/out/brain.html")
    if not isinstance(page_ref, dict):
        raise BundleValidationError("candidate release omitted site/out/brain.html")
    page_bytes = _read_physical_file(page_path, "shadow Brain page")
    if release_root is not None:
        source_page = _read_physical_file(
            release_root / "site" / "out" / "brain.html",
            "candidate frozen Brain page",
        )
        if page_bytes != source_page:
            raise BundleValidationError("shadow Brain page differs from the frozen candidate")
    if (
        page.get("bytes") != len(page_bytes)
        or page_ref.get("bytes") != len(page_bytes)
        or page.get("sha256") != hashlib.sha256(page_bytes).hexdigest()
        or page_ref.get("sha256") != hashlib.sha256(page_bytes).hexdigest()
    ):
        raise BundleValidationError("shadow Brain page digest/size evidence is inconsistent")

    selector_path = destination / "current.json"
    selector_raw = _read_physical_file(selector_path, "shadow Brain selector")
    selector = _parse_json_bytes(
        selector_raw, "shadow Brain selector", require_canonical=False
    )
    try:
        validate_release_selector(selector)
    except VerificationError as exc:
        raise BundleValidationError(f"shadow Brain selector is invalid: {exc}") from exc
    if selector.get("release_id") != release_id or selector.get("release") != release_hex:
        raise BundleValidationError("shadow Brain selector does not select the candidate release")
    previous_release_id = brain.get("previous_release_id")
    if selector.get("previous_release_id") != previous_release_id:
        if not (previous_release_id is None and "previous_release_id" not in selector):
            raise BundleValidationError(
                "shadow Brain selector previous release differs from the stage result"
            )

    expected_files: dict[str, tuple[int, str]] = {
        "current.json": (len(selector_raw), hashlib.sha256(selector_raw).hexdigest())
    }

    def add_namespace(
        namespace_id: str,
        manifest: Mapping[str, Any],
        manifest_bytes: bytes,
        *,
        candidate: bool,
    ) -> None:
        _, namespace_hex = _require_release_id(
            namespace_id, "shadow namespace release_id"
        )
        prefix = f"releases/{namespace_hex}/"
        expected_files[prefix + PUBLIC_RELEASE_MANIFEST] = (
            len(manifest_bytes),
            hashlib.sha256(manifest_bytes).hexdigest(),
        )
        for item in manifest.get("artifacts", []):
            if not isinstance(item, dict):
                continue
            source_relative = item.get("path")
            if not isinstance(source_relative, str):
                continue
            public_relative = _public_path_for_release_artifact(source_relative)
            if public_relative is None:
                continue
            size = _require_int(item.get("bytes"), f"release artifact {source_relative}.bytes")
            digest = _require_digest(
                item.get("sha256"), f"release artifact {source_relative}.sha256"
            )
            expected_files[prefix + public_relative] = (size, digest)
            if candidate:
                expected_files[public_relative] = (size, digest)

    add_namespace(
        release_id,
        release_manifest,
        release_manifest_bytes,
        candidate=True,
    )
    if previous_release_id is not None:
        previous_id, previous_hex = _require_release_id(
            previous_release_id, "shadow previous release_id"
        )
        previous_manifest_path = (
            destination / "releases" / previous_hex / PUBLIC_RELEASE_MANIFEST
        )
        previous_raw = _read_physical_file(
            previous_manifest_path, "shadow previous release manifest"
        )
        previous_manifest = _parse_json_bytes(
            previous_raw, "shadow previous release manifest", require_canonical=False
        )
        try:
            expected_previous_raw = release_canonical_json_bytes(previous_manifest)
        except VerificationError as exc:
            raise BundleValidationError(
                f"shadow previous release manifest is invalid: {exc}"
            ) from exc
        if previous_raw != expected_previous_raw:
            raise BundleValidationError(
                "shadow previous release manifest is not canonical release JSON"
            )
        try:
            validated_previous = validate_release_manifest(previous_manifest)
        except VerificationError as exc:
            raise BundleValidationError(
                f"shadow previous release manifest is invalid: {exc}"
            ) from exc
        if validated_previous.get("release_id") != previous_id:
            raise BundleValidationError("shadow previous namespace names the wrong release")
        add_namespace(
            previous_id,
            validated_previous,
            previous_raw,
            candidate=False,
        )

    actual_files = _scan_regular_tree(destination, "shadow Brain asset tree")
    if actual_files != expected_files:
        missing = sorted(set(expected_files) - set(actual_files))
        extra = sorted(set(actual_files) - set(expected_files))
        mismatched = sorted(
            path
            for path in set(actual_files) & set(expected_files)
            if actual_files[path] != expected_files[path]
        )
        raise BundleValidationError(
            "shadow Brain asset tree differs from its release manifests "
            f"(missing={missing}, extra={extra}, mismatched={mismatched})"
        )
    measured = [*actual_files.values(), (len(page_bytes), hashlib.sha256(page_bytes).hexdigest())]
    if (
        brain.get("objects") != len(measured)
        or brain.get("bytes") != sum(size for size, _ in measured)
        or brain.get("largest_file_bytes") != max(size for size, _ in measured)
        or brain.get("copy_buffer_bytes") != COPY_BUFFER_BYTES
    ):
        raise BundleValidationError("shadow Brain stage measurements differ from the staged bytes")


def _validate_release_metrics(
    document: Mapping[str, Any],
    *,
    release_id: str,
    expected_database: Path,
    expected_manifest: Path,
) -> None:
    _exact_keys(
        document,
        {
            "schema",
            "measured_at",
            "ok",
            "identity",
            "database",
            "counts",
            "analyze",
            "checks",
            "queries",
            "warnings",
            "duration_ms",
            "max_rss_bytes",
        },
        "release metrics",
    )
    identity = _require_object(document.get("identity"), "release metrics identity")
    database = _require_object(document.get("database"), "release metrics database")
    if (
        document.get("schema") != METRICS_SCHEMA
        or document.get("ok") is not True
        or document.get("warnings") != []
        or identity.get("release_id") != release_id
    ):
        raise BundleValidationError("release metrics are unhealthy or name the wrong release")
    _require_string(document.get("measured_at"), "release metrics measured_at")
    _require_number(document.get("duration_ms"), "release metrics duration_ms")
    _require_int(document.get("max_rss_bytes"), "release metrics max_rss_bytes", minimum=1)
    _exact_keys(
        identity,
        {
            "schema_version",
            "build_state",
            "snapshot_id",
            "base_snapshot_id",
            "projection_id",
            "release_id",
            "release_id_source",
            "snapshot_aliases_base",
            "snapshot_aliases_projection",
            "snapshot_id_alias",
        },
        "release metrics identity",
    )
    if identity.get("schema_version") != 2 or identity.get("build_state") != "complete":
        raise BundleValidationError("release metrics do not describe a complete schema-v2 snapshot")
    for field in ("snapshot_id", "base_snapshot_id", "projection_id"):
        _require_string(identity.get(field), f"release metrics identity.{field}")
    if identity.get("release_id_source") != str(expected_manifest):
        raise BundleValidationError("release metrics were not bound through the candidate manifest")
    for field in ("snapshot_aliases_base", "snapshot_aliases_projection"):
        _require_bool(identity.get(field), f"release metrics identity.{field}")
    base_alias = identity["snapshot_id"] == identity["base_snapshot_id"]
    projection_alias = identity["snapshot_id"] == identity["projection_id"]
    alias = identity.get("snapshot_id_alias")
    expected_alias = "base_snapshot_id" if base_alias else "projection_id" if projection_alias else "neither"
    if alias != expected_alias or alias == "neither":
        raise BundleValidationError("release metrics snapshot identity is inconsistent")
    if identity["snapshot_aliases_base"] != base_alias:
        raise BundleValidationError("release metrics base snapshot alias flag is inconsistent")
    if identity["snapshot_aliases_projection"] != projection_alias:
        raise BundleValidationError("release metrics projection alias flag is inconsistent")

    _exact_keys(
        database,
        {
            "path",
            "file_bytes",
            "application_id",
            "user_version",
            "page_size_bytes",
            "page_count",
            "allocated_bytes",
            "freelist_pages",
            "freelist_bytes",
            "used_pages",
            "used_bytes",
            "freelist_fraction",
            "journal_mode",
            "auto_vacuum",
            "query_only",
            "immutable",
            "read_only",
        },
        "release metrics database",
    )
    if database.get("path") != str(expected_database):
        raise BundleValidationError("release metrics name the wrong SQLite projection")
    if (
        database.get("application_id") != BRAIN_SQLITE_APPLICATION_ID
        or database.get("user_version") != 2
        or database.get("read_only") is not True
        or database.get("immutable") is not True
        or database.get("query_only") is not True
    ):
        raise BundleValidationError("release metrics do not describe an immutable schema-v2 store")
    for field in (
        "file_bytes",
        "page_size_bytes",
        "page_count",
        "allocated_bytes",
        "freelist_pages",
        "freelist_bytes",
        "used_pages",
        "used_bytes",
    ):
        _require_int(database.get(field), f"release metrics database.{field}")
    _require_number(database.get("freelist_fraction"), "release metrics database.freelist_fraction")
    _require_string(database.get("journal_mode"), "release metrics database.journal_mode")
    _require_int(database.get("auto_vacuum"), "release metrics database.auto_vacuum")
    if database["allocated_bytes"] != database["page_size_bytes"] * database["page_count"]:
        raise BundleValidationError("release metrics allocated byte count is inconsistent")
    if database["used_pages"] != database["page_count"] - database["freelist_pages"]:
        raise BundleValidationError("release metrics used page count is inconsistent")
    if database["used_bytes"] != database["page_size_bytes"] * database["used_pages"]:
        raise BundleValidationError("release metrics used byte count is inconsistent")
    if database["freelist_bytes"] != database["page_size_bytes"] * database["freelist_pages"]:
        raise BundleValidationError("release metrics freelist byte count is inconsistent")

    counts = _require_object(document.get("counts"), "release metrics counts")
    _exact_keys(counts, {"tables", "artifacts", "edges_by_stream"}, "release metrics counts")
    for name in ("tables", "artifacts", "edges_by_stream"):
        values = _require_object(counts.get(name), f"release metrics counts.{name}")
        if not values:
            raise BundleValidationError(f"release metrics counts.{name} must not be empty")
        for key, value in values.items():
            _require_string(key, f"release metrics counts.{name} key")
            _require_int(value, f"release metrics counts.{name}.{key}")
    if set(counts["tables"]) != {"nodes", "edges", "cells", "organ_owners", "synapses"}:
        raise BundleValidationError("release metrics table counts omit a schema-v2 core table")
    if not set(counts["edges_by_stream"]) <= {"main", "links"}:
        raise BundleValidationError("release metrics contain an unknown edge stream")

    analyze = _require_object(document.get("analyze"), "release metrics analyze")
    _exact_keys(
        analyze,
        {"present", "entry_count", "tables", "indexes", "entries"},
        "release metrics analyze",
    )
    if analyze.get("present") is not True:
        raise BundleValidationError("release metrics require persisted ANALYZE statistics")
    _require_int(analyze.get("entry_count"), "release metrics analyze.entry_count", minimum=1)
    if not isinstance(analyze.get("indexes"), list) or not analyze["indexes"]:
        raise BundleValidationError("release metrics analyze.indexes must not be empty")
    if not isinstance(analyze.get("tables"), list) or not analyze["tables"]:
        raise BundleValidationError("release metrics analyze.tables must not be empty")
    if not isinstance(analyze.get("entries"), list) or not analyze["entries"]:
        raise BundleValidationError("release metrics analyze.entries must not be empty")
    if analyze["entry_count"] != len(analyze["entries"]):
        raise BundleValidationError("release metrics analyze entry count is inconsistent")
    for name in ("tables", "indexes"):
        values = analyze[name]
        if not all(isinstance(item, str) and item for item in values):
            raise BundleValidationError(f"release metrics analyze.{name} must contain strings")
        if values != sorted(set(values)):
            raise BundleValidationError(f"release metrics analyze.{name} must be unique and sorted")
    for index, raw_entry in enumerate(analyze["entries"]):
        entry = _require_object(raw_entry, f"release metrics analyze.entries[{index}]")
        _exact_keys(
            entry,
            {"table", "index", "stat"},
            f"release metrics analyze.entries[{index}]",
        )
        _require_string(entry.get("table"), f"release metrics analyze.entries[{index}].table")
        if entry.get("index") is not None:
            _require_string(
                entry.get("index"), f"release metrics analyze.entries[{index}].index"
            )
        _require_string(entry.get("stat"), f"release metrics analyze.entries[{index}].stat")

    checks = _require_object(document.get("checks"), "release metrics checks")
    expected_checks = {"application_id", "identity", "quick_check", "integrity_check"}
    _exact_keys(checks, expected_checks, "release metrics checks")
    for name in expected_checks:
        check = _require_object(checks.get(name), f"release metrics checks.{name}")
        if check.get("ok") is not True:
            raise BundleValidationError(f"release metrics check {name} did not pass")
    _exact_keys(
        checks["application_id"],
        {"ok", "expected", "actual"},
        "release metrics checks.application_id",
    )
    if (
        checks["application_id"].get("expected") != BRAIN_SQLITE_APPLICATION_ID
        or checks["application_id"].get("actual") != BRAIN_SQLITE_APPLICATION_ID
    ):
        raise BundleValidationError("release metrics application-ID check is inconsistent")
    _exact_keys(checks["identity"], {"ok"}, "release metrics checks.identity")
    for name in ("quick_check", "integrity_check"):
        check = checks[name]
        _exact_keys(
            check,
            {"ok", "messages", "duration_ms", "error_limit"},
            f"release metrics checks.{name}",
        )
        if check.get("messages") != ["ok"]:
            raise BundleValidationError(f"release metrics check {name} is not clean")
        _require_number(check.get("duration_ms"), f"release metrics checks.{name}.duration_ms")
        error_limit = _require_int(
            check.get("error_limit"),
            f"release metrics checks.{name}.error_limit",
            minimum=1,
        )
        if error_limit != ACTIVATION_METRICS_CHECK_LIMIT:
            raise BundleValidationError(
                f"release metrics check {name} did not use the activation error limit"
            )

    queries = _require_object(document.get("queries"), "release metrics queries")
    expected_query_indexes = {
        "owner_lookup": [],
        "edge_neighborhood": ["edges_src_kind_idx", "edges_dst_kind_idx"],
        "synapse_neighborhood": ["synapses_src_idx", "synapses_dst_idx"],
    }
    _exact_keys(queries, set(expected_query_indexes), "release metrics queries")
    for name, required_indexes in expected_query_indexes.items():
        query = _require_object(queries.get(name), f"release metrics queries.{name}")
        _exact_keys(
            query,
            {
                "status",
                "sample_key",
                "limit",
                "iterations",
                "warmup_iterations",
                "rows_returned",
                "latency_ms",
                "plan",
                "plan_summary",
            },
            f"release metrics queries.{name}",
        )
        if query.get("status") not in {"ok", "empty"}:
            raise BundleValidationError(f"release metrics query {name} did not complete")
        sample_key = query.get("sample_key")
        if sample_key is not None:
            _require_string(sample_key, f"release metrics queries.{name}.sample_key")
        if (query.get("status") == "empty") != (sample_key is None):
            raise BundleValidationError(f"release metrics query {name} sample/status is inconsistent")
        limit = _require_int(
            query.get("limit"), f"release metrics queries.{name}.limit", minimum=1
        )
        iterations = _require_int(
            query.get("iterations"), f"release metrics queries.{name}.iterations", minimum=1
        )
        warmup = _require_int(
            query.get("warmup_iterations"),
            f"release metrics queries.{name}.warmup_iterations",
        )
        if (
            limit != ACTIVATION_METRICS_LIMIT
            or iterations != ACTIVATION_METRICS_ITERATIONS
            or warmup != ACTIVATION_METRICS_WARMUP
        ):
            raise BundleValidationError(
                f"release metrics query {name} did not use the activation probe settings"
            )
        _require_int(
            query.get("rows_returned"), f"release metrics queries.{name}.rows_returned"
        )
        latency = _require_object(
            query.get("latency_ms"), f"release metrics queries.{name}.latency_ms"
        )
        _exact_keys(
            latency,
            {"min", "p50", "p95", "mean", "max"},
            f"release metrics queries.{name}.latency_ms",
        )
        for statistic in ("min", "p50", "p95", "mean", "max"):
            _require_number(
                latency.get(statistic),
                f"release metrics queries.{name}.latency_ms.{statistic}",
            )
        if not isinstance(query.get("plan"), list) or not query["plan"]:
            raise BundleValidationError(f"release metrics query {name} omitted its plan")
        for index, raw_plan in enumerate(query["plan"]):
            plan_row = _require_object(
                raw_plan, f"release metrics queries.{name}.plan[{index}]"
            )
            _exact_keys(
                plan_row,
                {"id", "parent", "detail"},
                f"release metrics queries.{name}.plan[{index}]",
            )
            _require_int(plan_row.get("id"), f"release metrics queries.{name}.plan[{index}].id")
            _require_int(
                plan_row.get("parent"), f"release metrics queries.{name}.plan[{index}].parent"
            )
            _require_string(
                plan_row.get("detail"), f"release metrics queries.{name}.plan[{index}].detail"
            )
        plan = _require_object(
            query.get("plan_summary"), f"release metrics queries.{name}.plan_summary"
        )
        _exact_keys(
            plan,
            {
                "expected_indexes",
                "used_expected_indexes",
                "all_expected_indexes_used",
                "base_table_full_scans",
            },
            f"release metrics queries.{name}.plan_summary",
        )
        if (
            plan.get("all_expected_indexes_used") is not True
            or plan.get("base_table_full_scans") != []
        ):
            raise BundleValidationError(f"release metrics query {name} is not index-safe")
        for field in ("expected_indexes", "used_expected_indexes"):
            values = plan.get(field)
            if not isinstance(values, list) or not all(
                isinstance(item, str) and item for item in values
            ):
                raise BundleValidationError(
                    f"release metrics queries.{name}.plan_summary.{field} must be strings"
                )
            if values != list(dict.fromkeys(values)):
                raise BundleValidationError(
                    f"release metrics queries.{name}.plan_summary.{field} contains duplicates"
                )
        if plan["used_expected_indexes"] != plan["expected_indexes"]:
            raise BundleValidationError(
                f"release metrics query {name} did not use every expected index"
            )
        if plan["expected_indexes"] != required_indexes:
            raise BundleValidationError(
                f"release metrics query {name} expected-index contract drifted"
            )


def _stable_metrics_view(document: Mapping[str, Any]) -> dict[str, Any]:
    """Remove only observation-time fields before independent remeasurement."""
    def normalize(item: Any) -> Any:
        if isinstance(item, decimal.Decimal):
            return format(item.normalize(), "f")
        if isinstance(item, float):
            return format(decimal.Decimal(str(item)).normalize(), "f")
        if isinstance(item, list):
            return [normalize(child) for child in item]
        if isinstance(item, dict):
            return {key: normalize(child) for key, child in item.items()}
        return item

    value = normalize(document)
    for field in ("measured_at", "duration_ms", "max_rss_bytes"):
        value.pop(field, None)
    for name in ("quick_check", "integrity_check"):
        value["checks"][name].pop("duration_ms", None)
    for query in value["queries"].values():
        query.pop("latency_ms", None)
    return value


def _decimalize_measurement(value: Any) -> Any:
    """Convert finite runtime floats into the artifact JSON number model."""
    if isinstance(value, float):
        converted = decimal.Decimal(str(value))
        if not converted.is_finite():
            raise BundleValidationError("fresh release metrics contain a non-finite number")
        return converted
    if isinstance(value, list):
        return [_decimalize_measurement(item) for item in value]
    if isinstance(value, dict):
        return {key: _decimalize_measurement(item) for key, item in value.items()}
    return value


def _recompute_release_metrics(
    document: Mapping[str, Any],
    *,
    database: Path,
    release_id: str,
    manifest: Path,
) -> dict[str, Any]:
    try:
        measured = measure_store_tool.measure_database(
            database,
            release_id=release_id,
            release_id_source=str(manifest),
            limit=ACTIVATION_METRICS_LIMIT,
            iterations=ACTIVATION_METRICS_ITERATIONS,
            warmup=ACTIVATION_METRICS_WARMUP,
            check_limit=ACTIVATION_METRICS_CHECK_LIMIT,
        )
    except (OSError, ValueError, measure_store_tool.MeasurementError) as exc:
        raise BundleValidationError(f"cannot independently remeasure candidate SQLite: {exc}") from exc
    if _stable_metrics_view(measured) != _stable_metrics_view(document):
        raise BundleValidationError(
            "release metrics stable counts, checks, or query plans differ from remeasurement"
        )
    return _decimalize_measurement(measured)


def _validate_promoter_intent(
    dry_run: Mapping[str, Any],
    *,
    release_id: str,
    release_root: Path,
    release_manifest_sha256: str,
    authority_commit: str,
    reducer_commit: str,
    inspect_external: bool,
) -> dict[str, Any]:
    _exact_keys(
        dry_run,
        {"schema", "ok", "attempt_id", "proposed_intent", "production_mutated"},
        "promoter dry-run",
    )
    if (
        dry_run.get("schema") != DRY_RUN_SCHEMA
        or dry_run.get("ok") is not True
        or dry_run.get("production_mutated") is not False
    ):
        raise BundleValidationError("promoter dry-run is not a successful non-mutating result")
    attempt_id = _require_string(dry_run.get("attempt_id"), "promoter dry-run attempt_id")
    if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", attempt_id) is None:
        raise BundleValidationError("promoter dry-run attempt_id is malformed")
    intent = _require_object(dry_run.get("proposed_intent"), "promoter proposed_intent")
    _exact_keys(
        intent,
        {
            "requested_release_id",
            "release_root",
            "release_manifest_sha256",
            "release_tree",
            "authority_commit",
            "reducer_commit",
            "retained_release",
            "public_baseline",
            "public_tree",
            "public_result",
            "staged_selector",
            "worker_bundle",
            "audited_at",
            "base_url",
            "trust_source",
            "predeploy",
            "planned",
            "approval_note",
            "first_deploy_exception",
            "first_deploy_approval",
            "history",
            "retained_artifacts",
        },
        "promoter proposed_intent",
    )
    if (
        intent.get("requested_release_id") != release_id
        or intent.get("authority_commit") != authority_commit
        or intent.get("reducer_commit") != reducer_commit
        or intent.get("release_root") != str(release_root)
        or intent.get("release_manifest_sha256") != release_manifest_sha256
    ):
        raise BundleValidationError("promoter dry-run release identity is inconsistent")
    release_tree = _validate_tree_inventory(intent.get("release_tree"), "promoter release_tree")
    if release_tree.get("root") != str(release_root):
        raise BundleValidationError("promoter release_tree names the wrong root")
    if inspect_external and release_tree != _inventory_tree(release_root):
        raise BundleValidationError("promoter release_tree differs from the frozen candidate")
    public_tree = _validate_tree_inventory(intent.get("public_tree"), "promoter public_tree")

    public_baseline = _require_object(intent.get("public_baseline"), "promoter public_baseline")
    _exact_keys(
        public_baseline,
        {
            "baseline_id",
            "root",
            "manifest",
            "manifest_sha256",
            "authority_commit",
            "files",
            "bytes",
        },
        "promoter public_baseline",
    )
    _require_release_id(public_baseline.get("baseline_id"), "promoter public baseline_id")
    _declared_absolute_path(public_baseline.get("root"), "promoter public baseline root")
    _declared_absolute_path(
        public_baseline.get("manifest"), "promoter public baseline manifest"
    )
    _require_digest(
        public_baseline.get("manifest_sha256"), "promoter public baseline manifest digest"
    )
    _require_commit(
        public_baseline.get("authority_commit"), "promoter public baseline authority"
    )
    _require_int(public_baseline.get("files"), "promoter public baseline files")
    _require_int(public_baseline.get("bytes"), "promoter public baseline bytes")

    retained = intent.get("retained_release")
    if retained is not None:
        retained_value = _require_object(retained, "promoter retained_release")
        _exact_keys(
            retained_value,
            {
                "release_id",
                "release_root",
                "release_manifest_sha256",
                "release_tree",
                "authority_commit",
                "reducer_commit",
            },
            "promoter retained_release",
        )
        _require_release_id(retained_value.get("release_id"), "promoter retained release_id")
        _declared_absolute_path(retained_value.get("release_root"), "promoter retained release_root")
        _require_digest(
            retained_value.get("release_manifest_sha256"),
            "promoter retained release manifest digest",
        )
        _require_commit(retained_value.get("authority_commit"), "promoter retained authority")
        _require_commit(retained_value.get("reducer_commit"), "promoter retained reducer")
        _validate_tree_inventory(
            retained_value.get("release_tree"), "promoter retained release_tree"
        )

    staged = _require_object(intent.get("staged_selector"), "promoter staged_selector")
    _exact_keys(
        staged,
        {"sha256", "release_id", "previous_release_id", "audited_at"},
        "promoter staged_selector",
    )
    _require_digest(staged.get("sha256"), "promoter staged selector digest")
    if staged.get("release_id") != release_id:
        raise BundleValidationError("promoter staged selector names the wrong release")
    previous = staged.get("previous_release_id")
    if previous is not None:
        _require_release_id(previous, "promoter staged selector previous_release_id")
    staged_audited_at = _require_string(
        staged.get("audited_at"), "promoter staged selector audited_at"
    )
    retained_id = retained.get("release_id") if isinstance(retained, dict) else None
    if previous != retained_id:
        raise BundleValidationError("promoter staged previous release differs from retained release")

    worker = _require_object(intent.get("worker_bundle"), "promoter worker_bundle")
    _exact_keys(
        worker,
        {"tree", "entry", "config", "config_sha256", "node_version", "wrangler_version"},
        "promoter worker_bundle",
    )
    worker_tree = _validate_tree_inventory(worker.get("tree"), "promoter worker_bundle.tree")
    worker_root = _declared_absolute_path(
        worker_tree.get("root"), "promoter worker_bundle.tree.root"
    )
    worker_entry = _declared_absolute_path(worker.get("entry"), "promoter worker_bundle.entry")
    if not _is_within(worker_root, worker_entry):
        raise BundleValidationError("promoter Worker entry is outside the sealed bundle tree")
    _declared_absolute_path(worker.get("config"), "promoter worker_bundle.config")
    _require_digest(worker.get("config_sha256"), "promoter worker bundle config digest")
    node_version = _require_string(worker.get("node_version"), "promoter Node version")
    if re.fullmatch(r"v22\.[0-9]+\.[0-9]+", node_version) is None:
        raise BundleValidationError("promoter dry-run did not use Node 22")
    wrangler_version = _require_string(
        worker.get("wrangler_version"), "promoter Wrangler version"
    )
    if re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", wrangler_version) is None:
        raise BundleValidationError("promoter Wrangler version is malformed")

    predeploy = _require_object(intent.get("predeploy"), "promoter predeploy")
    _exact_keys(
        predeploy,
        {
            "deployment_id",
            "version_id",
            "status_sha256",
            "selector_status",
            "selector_sha256",
            "release_id",
            "previous_release_id",
            "audited_at",
        },
        "promoter predeploy",
    )
    for field in ("deployment_id", "version_id"):
        value = _require_string(predeploy.get(field), f"promoter predeploy.{field}")
        if UUID_RE.fullmatch(value) is None:
            raise BundleValidationError(f"promoter predeploy.{field} is malformed")
    _require_digest(predeploy.get("status_sha256"), "promoter predeploy status digest")
    _require_digest(predeploy.get("selector_sha256"), "promoter predeploy selector digest")
    if predeploy.get("selector_status") not in {200, 404}:
        raise BundleValidationError("promoter predeploy selector status must be 200 or 404")
    for field in ("release_id", "previous_release_id"):
        value = predeploy.get(field)
        if value is not None:
            _require_release_id(value, f"promoter predeploy.{field}")
    audited = predeploy.get("audited_at")
    if audited is not None:
        _require_string(audited, "promoter predeploy.audited_at")
    selector_status = predeploy.get("selector_status")
    if selector_status == 404:
        if any(
            predeploy.get(field) is not None
            for field in ("release_id", "previous_release_id", "audited_at")
        ):
            raise BundleValidationError("promoter missing-selector prestate contains release fields")
    else:
        if predeploy.get("release_id") is None:
            raise BundleValidationError("promoter live selector prestate omitted its release")
        expected_retained = (
            predeploy.get("previous_release_id")
            if predeploy.get("release_id") == release_id
            else predeploy.get("release_id")
        )
        if expected_retained != retained_id:
            raise BundleValidationError("promoter retained release differs from the live selector")

    planned = _require_object(intent.get("planned"), "promoter planned")
    _exact_keys(planned, {"tag", "message", "command_timeout_seconds"}, "promoter planned")
    expected_tag = f"brain-{release_id.removeprefix('sha256:')[:12]}-{attempt_id[-10:]}"
    expected_message = f"Brain release {release_id} attempt {attempt_id}"
    if planned.get("tag") != expected_tag or planned.get("message") != expected_message:
        raise BundleValidationError("promoter planned deployment annotations are inconsistent")
    try:
        command_timeout = decimal.Decimal(
            _require_string(
                planned.get("command_timeout_seconds"),
                "promoter planned command_timeout_seconds",
            )
        )
    except decimal.DecimalException as exc:
        raise BundleValidationError("promoter command timeout is malformed") from exc
    if not command_timeout.is_finite() or command_timeout <= 0:
        raise BundleValidationError("promoter command timeout must be finite and positive")

    if intent.get("base_url") != PRODUCTION_ORIGIN:
        raise BundleValidationError("promoter dry-run did not target the production origin")
    audited_at = _require_string(intent.get("audited_at"), "promoter audited_at")
    if staged_audited_at != audited_at:
        raise BundleValidationError("promoter audit timestamps are inconsistent")
    _require_string(intent.get("trust_source"), "promoter trust_source")
    approval_note = intent.get("approval_note")
    if approval_note is not None:
        _require_string(approval_note, "promoter approval_note")
    first_deploy = _require_bool(
        intent.get("first_deploy_exception"), "promoter first_deploy_exception"
    )
    first_approval = intent.get("first_deploy_approval")
    if first_deploy:
        _require_string(first_approval, "promoter first_deploy_approval")
    elif first_approval is not None:
        raise BundleValidationError(
            "promoter first_deploy_approval requires the recorded exception"
        )
    if (selector_status == 404) != first_deploy:
        raise BundleValidationError(
            "promoter first-deploy exception does not match selector presence"
        )

    history = _require_object(intent.get("history"), "promoter history")
    _exact_keys(history, {"deployments", "versions"}, "promoter history")
    for name in ("deployments", "versions"):
        evidence = _require_object(history.get(name), f"promoter history.{name}")
        _exact_keys(evidence, {"sha256", "bytes", "entries"}, f"promoter history.{name}")
        _require_digest(evidence.get("sha256"), f"promoter history.{name}.sha256")
        _require_int(evidence.get("bytes"), f"promoter history.{name}.bytes", minimum=1)
        entries = evidence.get("entries")
        if entries is not None:
            _require_int(entries, f"promoter history.{name}.entries")

    public_result = _require_object(intent.get("public_result"), "promoter public_result")
    public_dir = _declared_absolute_path(
        public_result.get("public_dir"), "promoter public_result.public_dir"
    )
    if public_tree.get("root") != str(public_dir):
        raise BundleValidationError("promoter public tree and public result roots differ")
    result_brain = _require_object(public_result.get("brain"), "promoter public_result.brain")
    expected_retained_ids = [release_id, *([previous] if previous is not None else [])]
    if (
        result_brain.get("previous_release_id") != previous
        or result_brain.get("retained_release_ids") != expected_retained_ids
    ):
        raise BundleValidationError("promoter public result retained releases are inconsistent")

    retained_artifacts = _require_object(
        intent.get("retained_artifacts"), "promoter retained_artifacts"
    )
    _exact_keys(
        retained_artifacts,
        {"schema", "artifact_id", "root", "manifest", "manifest_sha256"},
        "promoter retained_artifacts",
    )
    if retained_artifacts.get("schema") != DRY_RUN_ARTIFACT_SCHEMA:
        raise BundleValidationError("promoter retained artifact schema mismatch")
    artifact_id, artifact_hex = _require_release_id(
        retained_artifacts.get("artifact_id"), "promoter retained artifact_id"
    )
    retained_root = _declared_absolute_path(
        retained_artifacts.get("root"), "promoter retained artifact root"
    )
    retained_manifest = _declared_absolute_path(
        retained_artifacts.get("manifest"), "promoter retained artifact manifest"
    )
    if retained_root.name != artifact_hex or retained_manifest != retained_root / "manifest.json":
        raise BundleValidationError("promoter retained artifact paths differ from artifact_id")
    _require_digest(
        retained_artifacts.get("manifest_sha256"),
        "promoter retained artifact manifest digest",
    )
    if public_dir != retained_root / "public":
        raise BundleValidationError("promoter public result is outside retained artifacts")
    if worker_root != retained_root / "worker":
        raise BundleValidationError("promoter Worker bundle is outside retained artifacts")
    if _declared_absolute_path(worker.get("config"), "promoter worker_bundle.config") != retained_root / "wrangler.jsonc":
        raise BundleValidationError("promoter Wrangler config is outside retained artifacts")
    return intent


def _validate_retained_public_baseline(
    public_dir: Path,
    baseline_files: Sequence[PublicAssetFile],
) -> None:
    actual_all = _scan_regular_tree(public_dir, "retained promoter public tree")
    actual = {
        path: evidence
        for path, evidence in actual_all.items()
        if path != "brain.html"
        and path != "assets/brain"
        and not path.startswith("assets/brain/")
    }
    expected = {
        item.path: (item.bytes, item.sha256)
        for item in baseline_files
    }
    if actual != expected:
        missing = sorted(set(expected) - set(actual))
        extra = sorted(set(actual) - set(expected))
        mismatched = sorted(
            path
            for path in set(actual) & set(expected)
            if actual[path] != expected[path]
        )
        raise BundleValidationError(
            "retained promoter non-Brain public tree differs from the public baseline "
            f"(missing={missing}, extra={extra}, mismatched={mismatched})"
        )


def _validate_retained_promoter_artifacts(
    intent: Mapping[str, Any],
    *,
    attempt_id: str,
    release_id: str,
    authority_commit: str,
    release_root: Path | None,
    release_manifest: Mapping[str, Any],
    release_manifest_bytes: bytes,
    baseline_files: Sequence[PublicAssetFile],
    protected_roots: Sequence[Path],
) -> None:
    reference = _require_object(
        intent.get("retained_artifacts"), "promoter retained_artifacts"
    )
    root = _physical_absolute_path(
        reference.get("root"), "promoter retained artifact root", expect_dir=True
    )
    for protected in protected_roots:
        if _overlap(root, protected):
            raise BundleValidationError(
                "retained promoter artifacts must be outside worktrees and release inputs"
            )
    try:
        retained = verify_retained_dry_run_artifacts(
            root,
            expected_artifact_id=str(reference.get("artifact_id")),
        )
    except (OSError, PromotionError) as exc:
        raise BundleValidationError(f"retained promoter artifacts are invalid: {exc}") from exc
    if (
        retained.manifest != Path(str(reference.get("manifest")))
        or retained.manifest_sha256 != reference.get("manifest_sha256")
    ):
        raise BundleValidationError("retained promoter artifact reference is inconsistent")
    manifest, _ = _read_source_json(
        retained.manifest, "retained promoter artifact manifest"
    )
    if (
        manifest.get("attempt_id") != attempt_id
        or manifest.get("release_id") != release_id
        or manifest.get("authority_git_commit") != authority_commit
    ):
        raise BundleValidationError("retained promoter artifact identity is inconsistent")

    public_tree = _inventory_tree(retained.public_dir)
    worker_tree = _inventory_tree(retained.worker_dir)
    if intent.get("public_tree") != public_tree:
        raise BundleValidationError("retained promoter public tree differs from the intent")
    worker = _require_object(intent.get("worker_bundle"), "promoter worker_bundle")
    if worker.get("tree") != worker_tree or worker.get("entry") != str(retained.worker_entry):
        raise BundleValidationError("retained promoter Worker tree differs from the intent")
    config_body = _read_physical_file(retained.config, "retained Wrangler config")
    if (
        worker.get("config") != str(retained.config)
        or worker.get("config_sha256") != hashlib.sha256(config_body).hexdigest()
    ):
        raise BundleValidationError("retained promoter Wrangler config differs from the intent")
    _validate_retained_public_baseline(retained.public_dir, baseline_files)

    evidence = _require_object(manifest.get("evidence"), "retained promoter evidence")

    def evidence_body(name: str) -> bytes:
        record = _require_object(evidence.get(name), f"retained promoter evidence.{name}")
        path = _declared_absolute_path(
            str(retained.root / str(record.get("path"))),
            f"retained promoter evidence.{name}.path",
        )
        if not _is_within(retained.root / "evidence", path):
            raise BundleValidationError(f"retained promoter evidence {name} escaped its root")
        body = _read_physical_file(path, f"retained promoter evidence {name}")
        if (
            record.get("bytes") != len(body)
            or record.get("sha256") != hashlib.sha256(body).hexdigest()
        ):
            raise BundleValidationError(f"retained promoter evidence {name} digest mismatch")
        return body

    predeploy = _require_object(intent.get("predeploy"), "promoter predeploy")
    selector_body = evidence_body("initial_selector")
    if hashlib.sha256(selector_body).hexdigest() != predeploy.get("selector_sha256"):
        raise BundleValidationError("retained initial selector differs from promoter prestate")
    if predeploy.get("selector_status") == 200:
        selector = _parse_json_bytes(
            selector_body, "retained initial selector", require_canonical=False
        )
        try:
            validate_release_selector(selector)
        except VerificationError as exc:
            raise BundleValidationError(f"retained initial selector is invalid: {exc}") from exc
        if (
            selector.get("release_id") != predeploy.get("release_id")
            or selector.get("previous_release_id") != predeploy.get("previous_release_id")
            or selector.get("audited_at") != predeploy.get("audited_at")
        ):
            raise BundleValidationError("retained initial selector identity is inconsistent")

    status_before = evidence_body("status_before")
    status_after = evidence_body("status_after")
    if hashlib.sha256(status_before).hexdigest() != predeploy.get("status_sha256"):
        raise BundleValidationError("retained status-before differs from promoter prestate")
    try:
        before_state = parse_deployment_status(status_before)
        after_state = parse_deployment_status(status_after)
    except PromotionError as exc:
        raise BundleValidationError(f"retained Wrangler status is invalid: {exc}") from exc
    if (
        before_state.deployment_id != predeploy.get("deployment_id")
        or before_state.version_id != predeploy.get("version_id")
        or after_state.deployment_id != before_state.deployment_id
        or after_state.version_id != before_state.version_id
    ):
        raise BundleValidationError("retained Wrangler status sandwich is inconsistent")

    history = _require_object(intent.get("history"), "promoter history")
    for evidence_name, history_name in (
        ("deployments_history", "deployments"),
        ("versions_history", "versions"),
    ):
        body = evidence_body(evidence_name)
        recorded = _require_object(history.get(history_name), f"promoter history.{history_name}")
        try:
            value = extract_last_json_value(body, f"retained Wrangler {history_name} history")
        except PromotionError as exc:
            raise BundleValidationError(
                f"retained Wrangler {history_name} history is invalid: {exc}"
            ) from exc
        entries = len(value) if isinstance(value, list) else None
        if (
            recorded.get("sha256") != hashlib.sha256(body).hexdigest()
            or recorded.get("bytes") != len(body)
            or recorded.get("entries") != entries
        ):
            raise BundleValidationError(
                f"retained Wrangler {history_name} history differs from the intent"
            )

    staged = _require_object(intent.get("staged_selector"), "promoter staged_selector")
    staged_body = _read_physical_file(
        retained.public_dir / "assets" / "brain" / "current.json",
        "retained staged selector",
    )
    if hashlib.sha256(staged_body).hexdigest() != staged.get("sha256"):
        raise BundleValidationError(
            "retained staged selector bytes differ from the promoter intent"
        )
    staged_document = _parse_json_bytes(
        staged_body, "retained staged selector", require_canonical=False
    )
    try:
        validate_release_selector(staged_document)
    except VerificationError as exc:
        raise BundleValidationError(f"retained staged selector is invalid: {exc}") from exc
    if (
        staged_document.get("release_id") != staged.get("release_id")
        or staged_document.get("previous_release_id")
        != staged.get("previous_release_id")
        or staged_document.get("audited_at") != staged.get("audited_at")
    ):
        raise BundleValidationError(
            "retained staged selector identity differs from the promoter intent"
        )

    _validate_shadow_public_output(
        _require_object(intent.get("public_result"), "promoter public_result"),
        expected_public=retained.public_dir,
        release_root=release_root,
        release_manifest=release_manifest,
        release_manifest_bytes=release_manifest_bytes,
        release_id=release_id,
        release_hex=release_id.removeprefix("sha256:"),
    )


def _validate_semantic_diff(
    document: Mapping[str, Any],
    release_id: str,
    release_manifest: Mapping[str, Any],
    release_manifest_path: Path,
    baseline_manifest: Mapping[str, Any],
    baseline_manifest_path: Path,
) -> None:
    _exact_keys(
        document,
        {
            "schema",
            "from",
            "to",
            "coverage",
            "semantic_artifacts",
            "nodes",
            "edges",
            "snippets",
            "cells",
            "organ_membership",
            "frontier",
            "synapses",
            "frontier_graph",
            "summary",
            "different",
        },
        "semantic diff",
    )
    if document.get("schema") != SEMANTIC_DIFF_SCHEMA:
        raise BundleValidationError("semantic diff schema mismatch")
    before = _require_object(document.get("from"), "semantic diff from")
    after = _require_object(document.get("to"), "semantic diff to")
    _exact_keys(before, {"kind", "path", "release_id"}, "semantic diff from")
    _exact_keys(after, {"kind", "path", "release_id"}, "semantic diff to")
    if before.get("kind") != "release-manifest" or after.get("kind") != "release-manifest":
        raise BundleValidationError(
            "semantic diff must compare two complete release manifests"
        )
    baseline_release_id, baseline_release_hex = _require_release_id(
        baseline_manifest.get("release_id"), "semantic baseline release_id"
    )
    _require_release_id(before.get("release_id"), "semantic diff from.release_id")
    _require_release_id(after.get("release_id"), "semantic diff to.release_id")
    if before.get("release_id") != baseline_release_id:
        raise BundleValidationError("semantic diff source differs from the baseline release")
    if after.get("release_id") != release_id:
        raise BundleValidationError("semantic diff does not target the candidate release")
    declared_before = _declared_absolute_path(before.get("path"), "semantic diff from.path")
    declared_after = _declared_absolute_path(after.get("path"), "semantic diff to.path")
    if (
        declared_before != baseline_manifest_path
        or baseline_manifest_path.name != "release.json"
        or baseline_manifest_path.parent.name != baseline_release_hex
    ):
        raise BundleValidationError("semantic diff source path differs from the baseline release")
    if declared_after != release_manifest_path:
        raise BundleValidationError("semantic diff target path differs from the candidate release")

    required = list(COMPATIBILITY_SEMANTIC_PATHS)
    coverage = _require_object(document.get("coverage"), "semantic diff coverage")
    _exact_keys(coverage, {"required", "from", "to", "compared", "complete"}, "semantic diff coverage")
    if coverage.get("required") != required or coverage.get("compared") != required:
        raise BundleValidationError("semantic diff coverage must name all seven canonical paths in order")
    if coverage.get("complete") is not True:
        raise BundleValidationError("semantic diff coverage is incomplete")
    for side in ("from", "to"):
        side_value = _require_object(coverage.get(side), f"semantic diff coverage.{side}")
        _exact_keys(side_value, {"present", "missing", "complete"}, f"semantic diff coverage.{side}")
        if (
            side_value.get("present") != required
            or side_value.get("missing") != []
            or side_value.get("complete") is not True
        ):
            raise BundleValidationError(f"semantic diff {side} coverage is incomplete")

    semantic = _require_object(document.get("semantic_artifacts"), "semantic diff semantic_artifacts")
    if set(semantic) != set(required):
        raise BundleValidationError("semantic diff semantic_artifacts must contain exactly all seven paths")
    release_artifacts = {
        item.get("path"): item
        for item in release_manifest.get("artifacts", [])
        if isinstance(item, dict)
    }
    baseline_artifacts = {
        item.get("path"): item
        for item in baseline_manifest.get("artifacts", [])
        if isinstance(item, dict)
    }
    for path in required:
        record = _require_object(semantic.get(path), f"semantic diff semantic_artifacts[{path!r}]")
        _exact_keys(record, {"from", "to", "compared", "different"}, f"semantic artifact {path}")
        from_root = _require_digest(record.get("from"), f"semantic artifact {path}.from", prefixed=True)
        to_root = _require_digest(record.get("to"), f"semantic artifact {path}.to", prefixed=True)
        different = record.get("different")
        if record.get("compared") is not True or not isinstance(different, bool):
            raise BundleValidationError(f"semantic artifact {path} was not fully compared")
        expected = release_artifacts.get(path)
        if not isinstance(expected, dict) or expected.get("logical_root") != to_root:
            raise BundleValidationError(f"semantic artifact {path} target root differs from release")
        expected_before = baseline_artifacts.get(path)
        if not isinstance(expected_before, dict) or expected_before.get("logical_root") != from_root:
            raise BundleValidationError(f"semantic artifact {path} source root differs from release")

    section_keys = {
        "nodes": {"added", "removed", "changed"},
        "edges": {
            "added",
            "removed",
            "changed",
            "provenance_only",
            "grouped_by_source_kind",
            "compared_artifacts",
        },
        "snippets": {"added", "removed", "changed", "grouped_by_field_source"},
        "cells": {"added", "removed", "changed"},
        "organ_membership": {
            "added",
            "removed",
            "moved",
            "changed",
            "provenance_only",
            "splits",
            "merges",
        },
        "frontier": {"added", "removed", "changed"},
    }
    for name, keys in section_keys.items():
        section = _require_object(document.get(name), f"semantic diff {name}")
        _exact_keys(section, keys, f"semantic diff {name}")
        for key in keys:
            if not isinstance(section.get(key), list):
                raise BundleValidationError(f"semantic diff {name}.{key} must be an array")
    expected_edge_artifacts = [
        "brain/data/edges.jsonl",
        "brain/data/edges_links.jsonl",
    ]
    if document["edges"]["compared_artifacts"] != expected_edge_artifacts:
        raise BundleValidationError("semantic diff did not compare both edge streams")
    for name, path in (
        ("synapses", "brain/data/synapses.jsonl"),
        ("frontier_graph", "brain/data/frontier_graph.json"),
    ):
        section = _require_object(document.get(name), f"semantic diff {name}")
        _exact_keys(section, {"compared", "from", "to", "different"}, f"semantic diff {name}")
        if section != semantic[path]:
            raise BundleValidationError(
                f"semantic diff {name} summary differs from semantic_artifacts"
            )

    summary = _require_object(document.get("summary"), "semantic diff summary")
    _exact_keys(summary, set(section_keys) | {"synapses", "frontier_graph"}, "semantic diff summary")
    summary_keys = {
        "nodes": {"added", "removed", "changed"},
        "edges": {"added", "removed", "changed", "provenance_only"},
        "snippets": {"added", "removed", "changed"},
        "cells": {"added", "removed", "changed"},
        "organ_membership": {
            "added",
            "removed",
            "moved",
            "changed",
            "provenance_only",
            "splits",
            "merges",
        },
        "frontier": {"added", "removed", "changed"},
        "synapses": {"changed"},
        "frontier_graph": {"changed"},
    }
    for name, keys in summary_keys.items():
        section = _require_object(summary.get(name), f"semantic diff summary.{name}")
        _exact_keys(section, keys, f"semantic diff summary.{name}")
        for key in keys:
            _require_int(section.get(key), f"semantic diff summary.{name}.{key}")
    try:
        recomputed_summary = semantic_diff_tool.summarize_report(document)
        recomputed_different = semantic_diff_tool.summary_has_differences(recomputed_summary)
    except (KeyError, TypeError, ValueError) as exc:
        raise BundleValidationError(f"semantic diff detail is malformed: {exc}") from exc
    if summary != recomputed_summary:
        raise BundleValidationError("semantic diff summary disagrees with its detail")
    if not isinstance(document.get("different"), bool) or document.get("different") != recomputed_different:
        raise BundleValidationError("semantic diff different flag disagrees with its summary")


def _validate_evidence(
    documents: Mapping[str, Mapping[str, Any]],
    canonical: Mapping[str, bytes],
    *,
    inspect_external: bool,
    expected_semantic_baseline_id: str,
    external_paths: Mapping[str, Path] | None = None,
    git: Path | None = None,
) -> ValidatedInputs:
    release_manifest = documents["candidate_release_manifest"]
    if release_manifest.get("schema") != RELEASE_SCHEMA:
        raise BundleValidationError("candidate release manifest schema mismatch")
    try:
        validated_release = validate_release_manifest(dict(release_manifest))
    except VerificationError as exc:
        raise BundleValidationError(f"candidate release manifest is invalid: {exc}") from exc
    release_id, release_hex = _require_release_id(
        validated_release.get("release_id"), "candidate release_id"
    )
    authority = _require_object(validated_release.get("authority"), "release authority")
    reducer = _require_object(validated_release.get("reducer"), "release reducer")
    authority_commit = _require_commit(authority.get("git_commit"), "release authority commit")
    reducer_commit = _require_commit(reducer.get("git_commit"), "release reducer commit")
    if reducer_commit != authority_commit:
        raise BundleValidationError("candidate authority and reducer commits must agree")

    semantic_baseline_manifest = documents["semantic_baseline_manifest"]
    if semantic_baseline_manifest.get("schema") != RELEASE_SCHEMA:
        raise BundleValidationError("semantic baseline release manifest schema mismatch")
    try:
        validated_semantic_baseline = validate_release_manifest(
            dict(semantic_baseline_manifest)
        )
    except VerificationError as exc:
        raise BundleValidationError(
            f"semantic baseline release manifest is invalid: {exc}"
        ) from exc
    semantic_baseline_id, semantic_baseline_hex = _require_release_id(
        validated_semantic_baseline.get("release_id"), "semantic baseline release_id"
    )
    if semantic_baseline_id != expected_semantic_baseline_id:
        raise BundleValidationError(
            "semantic baseline release differs from the explicitly reviewed baseline ID"
        )
    if semantic_baseline_id == release_id:
        raise BundleValidationError("semantic baseline release must differ from the candidate")

    build_root, promotion_root = _validate_build_context(
        documents["build_context"],
        authority_commit,
        inspect_external=inspect_external,
        git=git,
    )
    ci_evidence = documents["ci_evidence"]
    if ci_evidence.get("schema") != CI_EVIDENCE_SCHEMA:
        raise BundleValidationError("activation CI evidence schema mismatch")
    try:
        validate_ci_evidence(
            ci_evidence,
            expected_repo_root=promotion_root,
            expected_git_commit=authority_commit,
        )
    except ActivationCIError as exc:
        raise BundleValidationError(f"activation CI evidence is invalid: {exc}") from exc
    if inspect_external:
        if git is None:
            raise BundleValidationError("approved Git executable is required during bundle freeze")
        ci_tools = _require_object(ci_evidence.get("tools"), "activation CI tools")
        ci_git = _require_object(ci_tools.get("git"), "activation CI Git tool")
        if ci_git.get("path") != str(git):
            raise BundleValidationError(
                "activation CI Git executable differs from the bundle inspection executable"
            )

    release_result = documents["release_result"]
    expected_artifacts = validated_release.get("artifacts", [])
    _validate_release_result(
        release_result,
        release_id=release_id,
        release_hex=release_hex,
        artifact_count=len(expected_artifacts),
        byte_count=sum(item["bytes"] for item in expected_artifacts),
    )
    path_validator = _physical_absolute_path if inspect_external else _declared_absolute_path
    if inspect_external:
        release_root = path_validator(
            release_result.get("root"), "release result root", expect_dir=True
        )
        manifest_path = path_validator(
            release_result.get("manifest"), "release result manifest", expect_dir=False
        )
    else:
        release_root = path_validator(release_result.get("root"), "release result root")
        manifest_path = path_validator(release_result.get("manifest"), "release result manifest")
    if manifest_path != release_root / "release.json" or release_root.name != release_hex:
        raise BundleValidationError("release result paths do not match the release identity")
    if inspect_external and _overlap(release_root, promotion_root):
        raise BundleValidationError(
            "candidate release root must be outside the promotion worktree"
        )
    expected_release_bytes = release_canonical_json_bytes(validated_release)
    if inspect_external:
        try:
            actual_document, _ = load_canonical_json(manifest_path)
            actual_validated = validate_release_manifest(actual_document)
            verify_release_files(actual_validated, release_root)
        except (OSError, VerificationError) as exc:
            raise BundleValidationError(f"candidate release verification failed: {exc}") from exc
        if actual_validated != validated_release:
            raise BundleValidationError("candidate release manifest copy differs from the frozen release")

    if inspect_external:
        if external_paths is None:
            raise BundleValidationError("external evidence paths are required during freeze")
        semantic_baseline_path = _physical_absolute_path(
            str(external_paths["semantic_baseline_manifest"]),
            "semantic baseline release manifest",
            expect_dir=False,
        )
        semantic_baseline_root = semantic_baseline_path.parent
        if (
            semantic_baseline_path.name != "release.json"
            or semantic_baseline_root.name != semantic_baseline_hex
        ):
            raise BundleValidationError(
                "semantic baseline manifest path does not match its release identity"
            )
        if _overlap(semantic_baseline_root, promotion_root):
            raise BundleValidationError(
                "semantic baseline release root must be outside the promotion worktree"
            )
        try:
            actual_baseline_document, _ = load_canonical_json(semantic_baseline_path)
            actual_baseline_validated = validate_release_manifest(actual_baseline_document)
            verify_release_files(actual_baseline_validated, semantic_baseline_root)
        except (OSError, VerificationError) as exc:
            raise BundleValidationError(
                f"semantic baseline release verification failed: {exc}"
            ) from exc
        if actual_baseline_validated != validated_semantic_baseline:
            raise BundleValidationError(
                "semantic baseline manifest copy differs from the frozen release"
            )
    else:
        semantic_from = _require_object(
            documents["semantic_diff"].get("from"), "semantic diff from"
        )
        semantic_baseline_path = _declared_absolute_path(
            semantic_from.get("path"), "semantic diff from.path"
        )
    baseline_manifest = documents["public_baseline_manifest"]
    try:
        baseline_id, baseline_authority, baseline_file_records = validate_public_baseline_manifest(
            baseline_manifest
        )
    except BaselineValidationError as exc:
        raise BundleValidationError(f"public baseline manifest is invalid: {exc}") from exc
    _, baseline_hex = _require_release_id(baseline_id, "public baseline_id")
    if baseline_authority != authority_commit:
        raise BundleValidationError("public baseline authority differs from the candidate release")
    baseline_file_count = len(baseline_file_records)
    baseline_byte_count = sum(item.bytes for item in baseline_file_records)

    source_attestation = documents["source_attestation"]
    _exact_keys(source_attestation, {"schema", "files"}, "source attestation")
    if source_attestation.get("schema") != SOURCE_ATTESTATION_SCHEMA:
        raise BundleValidationError("source attestation schema mismatch")
    expected_attestation_files = [
        {"path": item.path, "sha256": item.sha256, "bytes": item.bytes}
        for item in baseline_file_records
    ]
    if source_attestation.get("files") != expected_attestation_files:
        raise BundleValidationError("source attestation inventory differs from the public baseline")

    dry_run = documents["promoter_dry_run"]
    intent = _validate_promoter_intent(
        dry_run,
        release_id=release_id,
        release_root=release_root,
        release_manifest_sha256=hashlib.sha256(expected_release_bytes).hexdigest(),
        authority_commit=authority_commit,
        reducer_commit=reducer_commit,
        inspect_external=inspect_external,
    )
    retained = intent.get("retained_release")
    if isinstance(retained, dict) and retained.get("release_id") != semantic_baseline_id:
        raise BundleValidationError(
            "semantic baseline must equal the promoter's retained live release"
        )
    intent_baseline = _require_object(intent.get("public_baseline"), "promoter public_baseline")
    if inspect_external:
        baseline_root = _physical_absolute_path(
            intent_baseline.get("root"), "promoter public baseline root", expect_dir=True
        )
    else:
        baseline_root = _declared_absolute_path(
            intent_baseline.get("root"), "promoter public baseline root"
        )
    if baseline_root.name != baseline_hex:
        raise BundleValidationError("public baseline root basename differs from baseline_id")
    if inspect_external and _overlap(baseline_root, promotion_root):
        raise BundleValidationError(
            "public baseline root must be outside the promotion worktree"
        )
    expected_baseline_manifest = baseline_root / "manifest.json"
    if (
        intent_baseline.get("baseline_id") != baseline_id
        or intent_baseline.get("authority_commit") != authority_commit
        or intent_baseline.get("manifest") != str(expected_baseline_manifest)
        or intent_baseline.get("manifest_sha256")
        != hashlib.sha256(canonical["public_baseline_manifest"]).hexdigest()
    ):
        raise BundleValidationError("promoter dry-run baseline identity is inconsistent")
    baseline: PublicAssetBaseline | None = None
    verified_baseline_files = tuple(baseline_file_records)
    if inspect_external:
        if expected_baseline_manifest.read_bytes() != canonical["public_baseline_manifest"]:
            raise BundleValidationError("public baseline manifest copy differs from the frozen baseline")
        try:
            baseline = verify_public_baseline(
                baseline_root,
                promotion_root,
                expected_baseline_id=baseline_id,
                expected_authority_git_commit=authority_commit,
                git_executable=git,
            )
        except (OSError, BaselineValidationError) as exc:
            raise BundleValidationError(f"public baseline verification failed: {exc}") from exc
        if (
            len(baseline.files) != baseline_file_count
            or baseline.total_bytes != baseline_byte_count
            or tuple(baseline.files) != verified_baseline_files
        ):
            raise BundleValidationError("verified baseline inventory differs from its manifest")
    if (
        intent_baseline.get("files") != baseline_file_count
        or intent_baseline.get("bytes") != baseline_byte_count
    ):
        raise BundleValidationError("promoter dry-run baseline counts are inconsistent")

    fresh_metrics: dict[str, Any] | None = None
    if inspect_external:
        source_path = promotion_root / SOURCE_ATTESTATION_PATH
        if not source_path.is_file() or source_path.is_symlink():
            raise BundleValidationError("promotion worktree lacks the committed source attestation")
        if source_path.read_bytes() != canonical["source_attestation"]:
            raise BundleValidationError("source attestation copy differs from the clean promotion worktree")
        committed_source = _git(
            git,
            promotion_root,
            ["show", f"{authority_commit}:{SOURCE_ATTESTATION_PATH}"],
            "committed source attestation",
        )
        if committed_source != canonical["source_attestation"]:
            raise BundleValidationError("source attestation copy differs from the authority Git blob")

    _validate_retained_promoter_artifacts(
        intent,
        attempt_id=_require_string(
            dry_run.get("attempt_id"), "promoter dry-run attempt_id"
        ),
        release_id=release_id,
        authority_commit=authority_commit,
        release_root=release_root if inspect_external else None,
        release_manifest=validated_release,
        release_manifest_bytes=expected_release_bytes,
        baseline_files=verified_baseline_files,
        protected_roots=(
            (
                build_root,
                promotion_root,
                release_root,
                semantic_baseline_path.parent,
                baseline_root,
            )
            if inspect_external
            else ()
        ),
    )

    expected_database = release_root / "brain" / "data" / "brain.sqlite3"
    _validate_release_metrics(
        documents["release_metrics"],
        release_id=release_id,
        expected_database=expected_database,
        expected_manifest=manifest_path,
    )
    if inspect_external and documents["release_metrics"]["database"]["file_bytes"] != expected_database.stat().st_size:
        raise BundleValidationError("release metrics SQLite byte count differs from the frozen file")
    if inspect_external:
        fresh_metrics = _recompute_release_metrics(
            documents["release_metrics"],
            database=expected_database,
            release_id=release_id,
            manifest=manifest_path,
        )

    _validate_public_result(
        documents["shadow_public_result"],
        release_id=release_id,
        release_hex=release_hex,
        baseline_id=baseline_id,
        baseline_authority=baseline_authority,
        baseline_root=baseline_root,
        baseline_files=baseline_file_count,
        baseline_bytes=baseline_byte_count,
        require_baseline=False,
        label="shadow public result",
    )
    if inspect_external:
        _validate_shadow_public_output(
            documents["shadow_public_result"],
            expected_public=build_root / "wiki" / "public",
            release_root=release_root,
            release_manifest=validated_release,
            release_manifest_bytes=expected_release_bytes,
            release_id=release_id,
            release_hex=release_hex,
        )
    intent_public = _require_object(intent.get("public_result"), "promoter public_result")
    _validate_public_result(
        intent_public,
        release_id=release_id,
        release_hex=release_hex,
        baseline_id=baseline_id,
        baseline_authority=baseline_authority,
        baseline_root=baseline_root,
        baseline_files=baseline_file_count,
        baseline_bytes=baseline_byte_count,
        require_baseline=True,
        label="promoter public result",
    )
    if inspect_external:
        try:
            recomputed_semantic_diff = semantic_diff_tool.compare_paths(
                semantic_baseline_path,
                manifest_path,
            )
        except (OSError, semantic_diff_tool.SemanticDiffError) as exc:
            raise BundleValidationError(
                f"cannot recompute semantic comparison: {exc}"
            ) from exc
        recomputed_bytes = _canonical_json_bytes(recomputed_semantic_diff)
        if recomputed_bytes != canonical["semantic_diff"]:
            raise BundleValidationError(
                "semantic diff evidence differs from a fresh comparison of the frozen releases"
            )
    _validate_semantic_diff(
        documents["semantic_diff"],
        release_id,
        validated_release,
        manifest_path,
        validated_semantic_baseline,
        semantic_baseline_path,
    )

    frozen_documents: dict[str, Mapping[str, Any]] = dict(documents)
    frozen_canonical: dict[str, bytes] = dict(canonical)
    if fresh_metrics is not None:
        frozen_documents["release_metrics"] = fresh_metrics
        frozen_canonical["release_metrics"] = _canonical_json_bytes(fresh_metrics)

    return ValidatedInputs(
        release_id=release_id,
        release_hex=release_hex,
        semantic_baseline_release_id=semantic_baseline_id,
        baseline_id=baseline_id,
        authority_commit=authority_commit,
        reducer_commit=reducer_commit,
        release_root=release_root,
        semantic_baseline_root=semantic_baseline_path.parent,
        baseline_root=baseline_root,
        build_root=build_root,
        promotion_root=promotion_root,
        git=git,
        baseline_files=verified_baseline_files,
        documents=frozen_documents,
        canonical_bytes=frozen_canonical,
    )


def _load_inputs(
    paths: Mapping[str, Path],
    *,
    ci_evidence: Mapping[str, Any],
    expected_semantic_baseline_id: str,
    git: Path,
) -> ValidatedInputs:
    expected_semantic_baseline_id, _ = _require_release_id(
        expected_semantic_baseline_id,
        "expected semantic baseline ID",
    )
    expected_external = {kind for kind, _ in EXTERNAL_EVIDENCE_PATHS}
    if set(paths) != expected_external:
        raise BundleValidationError("activation evidence input set is incomplete")
    documents: dict[str, Mapping[str, Any]] = {}
    canonical: dict[str, bytes] = {}
    for kind, _ in EXTERNAL_EVIDENCE_PATHS:
        document, encoded = _read_source_json(paths[kind], kind.replace("_", " "))
        documents[kind] = document
        canonical[kind] = encoded
    ci_bytes = _canonical_json_bytes(dict(ci_evidence))
    documents["ci_evidence"] = _parse_json_bytes(
        ci_bytes, "fresh activation CI evidence", require_canonical=True
    )
    canonical["ci_evidence"] = ci_bytes
    return _validate_evidence(
        documents,
        canonical,
        inspect_external=True,
        expected_semantic_baseline_id=expected_semantic_baseline_id,
        external_paths=paths,
        git=git,
    )


def _identity_document(validated: ValidatedInputs, files: Sequence[EvidenceFile]) -> dict[str, object]:
    return {
        "schema": BUNDLE_SCHEMA,
        "release_id": validated.release_id,
        "semantic_baseline_release_id": validated.semantic_baseline_release_id,
        "baseline_id": validated.baseline_id,
        "authority": {
            "git_commit": validated.authority_commit,
            "reducer_git_commit": validated.reducer_commit,
        },
        "files": [
            {
                "kind": item.kind,
                "path": item.path,
                "sha256": item.sha256,
                "bytes": item.bytes,
            }
            for item in files
        ],
    }


def _bundle_id(identity: Mapping[str, object]) -> str:
    payload = (
        b"wikilean\0"
        + BUNDLE_DOMAIN.encode("ascii")
        + b"\0canonical-json-v1\0"
        + _canonical_json_bytes(identity).removesuffix(b"\n")
    )
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def _write_new(path: Path, raw: bytes) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags, 0o600)
    try:
        view = memoryview(raw)
        while view:
            written = os.write(descriptor, view)
            if written <= 0:
                raise BundleFreezeError(f"short write while publishing {path.name}")
            view = view[written:]
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _seal(root: Path) -> None:
    for child in root.iterdir():
        info = child.lstat()
        if not stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode):
            raise BundleFreezeError(f"pending bundle contains unsafe entry: {child.name}")
        child.chmod(0o444)
        descriptor = os.open(child, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    root.chmod(0o555)
    _fsync_directory(root)


def _remove_pending(path: Path) -> None:
    if not path.exists() and not path.is_symlink():
        return
    with contextlib.suppress(OSError):
        path.chmod(0o700)
    if path.is_dir() and not path.is_symlink():
        for child in path.iterdir():
            with contextlib.suppress(OSError):
                child.chmod(0o600)
            with contextlib.suppress(OSError):
                child.unlink()
        with contextlib.suppress(OSError):
            path.rmdir()
    else:
        with contextlib.suppress(OSError):
            path.unlink()


def _prepare_store(raw: Path, validated: ValidatedInputs) -> Path:
    if not raw.is_absolute():
        raise BundleValidationError("output store must be absolute")
    if raw.exists() or raw.is_symlink():
        if raw.is_symlink() or not raw.is_dir():
            raise BundleValidationError("output store must be a real directory")
        store = raw.resolve(strict=True)
        if str(store) != str(raw):
            raise BundleValidationError("output store must be its physical canonical path")
    else:
        parent = raw.parent.resolve(strict=True)
        if str(parent / raw.name) != str(raw):
            raise BundleValidationError("output store must be its physical canonical path")
        prospective = parent / raw.name
        protected_roots = (
            validated.build_root,
            validated.promotion_root,
            validated.release_root,
            validated.semantic_baseline_root,
            validated.baseline_root,
        )
        for protected in protected_roots:
            if _overlap(prospective, protected):
                raise BundleValidationError(
                    "output store must be outside and non-overlapping with worktrees and inputs"
                )
        raw.mkdir(mode=0o700)
        _fsync_directory(parent)
        store = raw.resolve(strict=True)
    store_info = store.stat()
    if store_info.st_uid != os.geteuid():
        raise BundleValidationError("output store must be owned by the current user")
    if stat.S_IMODE(store_info.st_mode) & 0o077:
        raise BundleValidationError("output store must not grant group or other permissions")
    if not os.access(store, os.R_OK | os.W_OK | os.X_OK):
        raise BundleValidationError("output store must be readable, writable, and searchable")
    for protected in (
        validated.build_root,
        validated.promotion_root,
        validated.release_root,
        validated.semantic_baseline_root,
        validated.baseline_root,
    ):
        if _overlap(store, protected):
            raise BundleValidationError(
                "output store must be outside and non-overlapping with worktrees and inputs"
            )
    return store


@contextlib.contextmanager
def _store_lock(store: Path):
    lock = store / ".activation-bundle.lock"
    descriptor = os.open(lock, os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise BundleFreezeError("activation bundle lock must be a single-link regular file")
        if stat.S_IMODE(info.st_mode) & 0o077:
            raise BundleFreezeError("activation bundle lock must not grant group or other permissions")
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


def _final_publish_fence(validated: ValidatedInputs) -> None:
    """Repeat mutable authority/input checks immediately before publication."""
    if validated.git is None:
        raise BundleFreezeError("approved Git executable is missing from the publish fence")
    build_root, promotion_root = _validate_build_context(
        validated.documents["build_context"],
        validated.authority_commit,
        inspect_external=True,
        git=validated.git,
    )
    if build_root != validated.build_root or promotion_root != validated.promotion_root:
        raise BundleFreezeError("worktree identity changed before bundle publication")
    intent = _require_object(
        validated.documents["promoter_dry_run"].get("proposed_intent"),
        "promoter proposed_intent",
    )
    if _inventory_tree(validated.release_root) != intent.get("release_tree"):
        raise BundleFreezeError("candidate release changed before bundle publication")
    candidate_manifest = validated.documents["candidate_release_manifest"]
    _validate_retained_promoter_artifacts(
        intent,
        attempt_id=_require_string(
            validated.documents["promoter_dry_run"].get("attempt_id"),
            "promoter dry-run attempt_id",
        ),
        release_id=validated.release_id,
        authority_commit=validated.authority_commit,
        release_root=validated.release_root,
        release_manifest=candidate_manifest,
        release_manifest_bytes=release_canonical_json_bytes(dict(candidate_manifest)),
        baseline_files=validated.baseline_files,
        protected_roots=(
            validated.build_root,
            validated.promotion_root,
            validated.release_root,
            validated.semantic_baseline_root,
            validated.baseline_root,
        ),
    )


def freeze_activation_bundle(
    paths: Mapping[str, Path],
    output_store: Path,
    *,
    ci_evidence: Mapping[str, Any],
    expected_semantic_baseline_id: str,
    git: Path,
) -> ActivationBundle:
    approved_git = _approved_git_path(git)
    validated = _load_inputs(
        paths,
        ci_evidence=ci_evidence,
        expected_semantic_baseline_id=expected_semantic_baseline_id,
        git=approved_git,
    )
    store = _prepare_store(output_store, validated)
    files = tuple(
        EvidenceFile(
            kind=kind,
            path=destination,
            sha256=hashlib.sha256(validated.canonical_bytes[kind]).hexdigest(),
            bytes=len(validated.canonical_bytes[kind]),
        )
        for kind, destination in EVIDENCE_PATHS
    )
    identity = _identity_document(validated, files)
    bundle_id = _bundle_id(identity)
    bundle_hex = bundle_id.removeprefix("sha256:")
    manifest = {**identity, "bundle_id": bundle_id}
    pending = store / f".pending-{os.getpid()}-{uuid.uuid4().hex}"

    with _store_lock(store):
        try:
            pending.mkdir(mode=0o700)
            for item in files:
                _write_new(pending / item.path, validated.canonical_bytes[item.kind])
            _write_new(pending / MANIFEST_NAME, _canonical_json_bytes(manifest))
            _seal(pending)
            _final_publish_fence(validated)
            final = store / bundle_hex
            if final.exists() or final.is_symlink():
                existing = verify_activation_bundle(final)
                if existing.bundle_id != bundle_id:
                    raise BundleFreezeError("existing bundle directory has a different identity")
                _remove_pending(pending)
                return existing
            try:
                os.rename(pending, final)
            except OSError:
                if not final.is_dir() or final.is_symlink():
                    raise
                existing = verify_activation_bundle(final)
                if existing.bundle_id != bundle_id:
                    raise BundleFreezeError("concurrent bundle has a different identity")
                _remove_pending(pending)
                return existing
            _fsync_directory(store)
            return verify_activation_bundle(final)
        finally:
            _remove_pending(pending)


def _scan_bundle(root: Path) -> dict[str, os.stat_result]:
    if not root.is_absolute() or root.is_symlink():
        raise BundleValidationError("bundle root must be an absolute, non-symlink directory")
    resolved = root.resolve(strict=True)
    if resolved != root or not resolved.is_dir():
        raise BundleValidationError("bundle root must be its physical canonical directory")
    if stat.S_IMODE(root.stat().st_mode) != 0o555:
        raise BundleValidationError("bundle root permissions must be 0555")
    result: dict[str, os.stat_result] = {}
    before = root.stat()
    for child in root.iterdir():
        info = child.lstat()
        if stat.S_ISLNK(info.st_mode):
            raise BundleValidationError(f"bundle contains a symlink: {child.name}")
        if not stat.S_ISREG(info.st_mode):
            raise BundleValidationError(f"bundle contains a non-regular entry: {child.name}")
        if stat.S_IMODE(info.st_mode) != 0o444:
            raise BundleValidationError(f"bundle file permissions must be 0444: {child.name}")
        if info.st_nlink != 1:
            raise BundleValidationError(f"bundle files must not have external hard links: {child.name}")
        result[child.name] = info
    after = root.stat()
    if _signature(before) != _signature(after):
        raise BundleValidationError("bundle directory changed while scanning")
    return result


def _read_frozen_file(root: Path, name: str, maximum: int = MAX_JSON_BYTES) -> bytes:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(root / name, flags)
    try:
        before = os.fstat(descriptor)
        chunks: list[bytes] = []
        total = 0
        while True:
            chunk = os.read(descriptor, COPY_BUFFER_BYTES)
            if not chunk:
                break
            total += len(chunk)
            if total > maximum:
                raise BundleValidationError(f"bundle file exceeds size limit: {name}")
            chunks.append(chunk)
        after = os.fstat(descriptor)
    finally:
        os.close(descriptor)
    if _signature(before) != _signature(after) or total != before.st_size:
        raise BundleValidationError(f"bundle file changed while reading: {name}")
    return b"".join(chunks)


def _validate_manifest(
    document: Mapping[str, Any],
) -> tuple[str, str, str, str, str, tuple[EvidenceFile, ...]]:
    _exact_keys(
        document,
        {
            "schema",
            "bundle_id",
            "release_id",
            "semantic_baseline_release_id",
            "baseline_id",
            "authority",
            "files",
        },
        "bundle manifest",
    )
    if document.get("schema") != BUNDLE_SCHEMA:
        raise BundleValidationError("bundle manifest schema mismatch")
    bundle_id, _ = _require_release_id(document.get("bundle_id"), "bundle_id")
    release_id, _ = _require_release_id(document.get("release_id"), "bundle release_id")
    semantic_baseline_release_id, _ = _require_release_id(
        document.get("semantic_baseline_release_id"),
        "bundle semantic_baseline_release_id",
    )
    baseline_id, _ = _require_release_id(document.get("baseline_id"), "bundle baseline_id")
    authority = _require_object(document.get("authority"), "bundle authority")
    _exact_keys(authority, {"git_commit", "reducer_git_commit"}, "bundle authority")
    authority_commit = _require_commit(authority.get("git_commit"), "bundle authority commit")
    reducer_commit = _require_commit(authority.get("reducer_git_commit"), "bundle reducer commit")
    raw_files = document.get("files")
    if not isinstance(raw_files, list) or len(raw_files) != len(EVIDENCE_PATHS):
        raise BundleValidationError("bundle manifest must list the exact evidence file set")
    files: list[EvidenceFile] = []
    for index, raw in enumerate(raw_files):
        item = _require_object(raw, f"bundle files[{index}]")
        _exact_keys(item, {"kind", "path", "sha256", "bytes"}, f"bundle files[{index}]")
        kind = _require_string(item.get("kind"), f"bundle files[{index}].kind")
        path = _require_string(item.get("path"), f"bundle files[{index}].path")
        if (kind, path) != EVIDENCE_PATHS[index]:
            raise BundleValidationError("bundle evidence files must use the canonical order and names")
        digest = _require_digest(item.get("sha256"), f"bundle files[{index}].sha256")
        size = item.get("bytes")
        if isinstance(size, bool) or not isinstance(size, int) or size < 0:
            raise BundleValidationError(f"bundle files[{index}].bytes must be nonnegative")
        files.append(EvidenceFile(kind, path, digest, size))
    identity = dict(document)
    identity.pop("bundle_id")
    expected = _bundle_id(identity)
    if bundle_id != expected:
        raise BundleValidationError(f"bundle_id mismatch: expected {expected}")
    return (
        bundle_id,
        release_id,
        semantic_baseline_release_id,
        baseline_id,
        authority_commit,
        tuple(files),
    )


def verify_activation_bundle(root_input: Path) -> ActivationBundle:
    root = root_input
    actual = _scan_bundle(root)
    expected_names = {MANIFEST_NAME, *EVIDENCE_BY_PATH}
    if set(actual) != expected_names:
        missing = sorted(expected_names - set(actual))
        extra = sorted(set(actual) - expected_names)
        raise BundleValidationError(
            f"bundle file closure mismatch (missing={missing}, extra={extra})"
        )
    manifest_raw = _read_frozen_file(root, MANIFEST_NAME)
    manifest = _parse_json_bytes(manifest_raw, "bundle manifest", require_canonical=True)
    (
        bundle_id,
        release_id,
        semantic_baseline_release_id,
        baseline_id,
        authority_commit,
        files,
    ) = _validate_manifest(manifest)
    bundle_hex = bundle_id.removeprefix("sha256:")
    if root.name != bundle_hex:
        raise BundleValidationError("bundle root basename differs from bundle_id")

    documents: dict[str, Mapping[str, Any]] = {}
    canonical: dict[str, bytes] = {}
    for item in files:
        raw = _read_frozen_file(root, item.path)
        if len(raw) != item.bytes or hashlib.sha256(raw).hexdigest() != item.sha256:
            raise BundleValidationError(f"bundle evidence digest/size mismatch: {item.path}")
        document = _parse_json_bytes(raw, item.path, require_canonical=True)
        documents[item.kind] = document
        canonical[item.kind] = raw
    validated = _validate_evidence(
        documents,
        canonical,
        inspect_external=False,
        expected_semantic_baseline_id=semantic_baseline_release_id,
    )
    if (
        validated.release_id != release_id
        or validated.semantic_baseline_release_id != semantic_baseline_release_id
        or validated.baseline_id != baseline_id
        or validated.authority_commit != authority_commit
        or validated.reducer_commit != manifest["authority"]["reducer_git_commit"]
    ):
        raise BundleValidationError("bundle manifest identity differs from its evidence")
    final = _scan_bundle(root)
    if {name: _signature(info) for name, info in final.items()} != {
        name: _signature(info) for name, info in actual.items()
    }:
        raise BundleValidationError("bundle changed while verifying")
    return ActivationBundle(
        root=root,
        manifest_path=root / MANIFEST_NAME,
        bundle_id=bundle_id,
        bundle_hex=bundle_hex,
        release_id=release_id,
        semantic_baseline_release_id=semantic_baseline_release_id,
        baseline_id=baseline_id,
        authority_git_commit=authority_commit,
        reducer_git_commit=validated.reducer_commit,
        files=files,
    )


def _describe_worktree(
    root_input: Path, label: str, *, git: Path
) -> dict[str, object]:
    root = _physical_absolute_path(str(root_input), f"{label} root", expect_dir=True)
    head = (
        _git(git, root, ["rev-parse", "HEAD"], f"{label} HEAD")
        .decode("ascii", errors="strict")
        .strip()
    )
    _require_commit(head, f"{label} HEAD")
    branch_name = (
        _git(git, root, ["rev-parse", "--abbrev-ref", "HEAD"], f"{label} branch")
        .decode("utf-8", errors="strict")
        .strip()
    )
    branch = "detached" if branch_name == "HEAD" else branch_name
    clean = not _git(
        git,
        root,
        ["status", "--porcelain=v1", "--untracked-files=all"],
        f"{label} status",
    ).strip()
    return {"root": str(root), "head": head, "branch": branch, "clean": clean}


def create_build_context(
    build_worktree: Path, promotion_worktree: Path, *, git: Path
) -> dict[str, object]:
    """Describe and validate the two isolated worktrees used for P1B."""
    approved_git = _approved_git_path(git)
    promotion = _describe_worktree(
        promotion_worktree, "promotion worktree", git=approved_git
    )
    authority = _require_commit(promotion["head"], "promotion worktree HEAD")
    document = {
        "schema": BUILD_CONTEXT_SCHEMA,
        "authority_git_commit": authority,
        "build_worktree": _describe_worktree(
            build_worktree, "build worktree", git=approved_git
        ),
        "promotion_worktree": promotion,
    }
    _validate_build_context(
        document, authority, inspect_external=True, git=approved_git
    )
    return document


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    freeze = subparsers.add_parser("freeze", help="verify inputs and atomically freeze a bundle")
    freeze.add_argument("--release-manifest", type=Path, required=True)
    freeze.add_argument("--semantic-baseline-manifest", type=Path, required=True)
    freeze.add_argument("--expected-semantic-baseline-id", required=True)
    freeze.add_argument("--public-baseline-manifest", type=Path, required=True)
    freeze.add_argument("--source-attestation", type=Path, required=True)
    freeze.add_argument("--release-result", type=Path, required=True)
    freeze.add_argument("--release-metrics", type=Path, required=True)
    freeze.add_argument("--shadow-public-result", type=Path, required=True)
    freeze.add_argument("--semantic-diff", type=Path, required=True)
    freeze.add_argument("--promoter-dry-run", type=Path, required=True)
    freeze.add_argument("--build-context", type=Path, required=True)
    freeze.add_argument("--git", type=Path, required=True)
    freeze.add_argument("--node", type=Path, required=True)
    freeze.add_argument("--npm", type=Path, required=True)
    freeze.add_argument("--python", type=Path, required=True)
    freeze.add_argument(
        "--ci-command-timeout",
        type=float,
        default=3600.0,
        help="per-command timeout for the fresh in-process CI recording",
    )
    freeze.add_argument("--output-store", type=Path, required=True)
    verify = subparsers.add_parser("verify", help="verify a frozen activation bundle")
    verify.add_argument("--bundle-root", type=Path, required=True)
    verify.add_argument(
        "--expected-bundle-id",
        help="optional external trust anchor; must equal the verified bundle ID",
    )
    verify.add_argument("--expected-semantic-baseline-id")
    context = subparsers.add_parser(
        "context", help="emit a verified canonical two-worktree context document"
    )
    context.add_argument("--build-worktree", type=Path, required=True)
    context.add_argument("--promotion-worktree", type=Path, required=True)
    context.add_argument("--git", type=Path, required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "freeze":
            if (
                not isinstance(args.ci_command_timeout, float)
                or not args.ci_command_timeout > 0
                or not decimal.Decimal(str(args.ci_command_timeout)).is_finite()
            ):
                raise BundleValidationError("CI command timeout must be finite and positive")
            expected_semantic_baseline_id, _ = _require_release_id(
                args.expected_semantic_baseline_id,
                "expected semantic baseline ID",
            )
            fresh_ci = record_activation_ci(
                repo_root=REPO_ROOT,
                git=args.git,
                node=args.node,
                npm=args.npm,
                python=args.python,
                command_timeout=args.ci_command_timeout,
            )
            paths = {
                "candidate_release_manifest": args.release_manifest,
                "semantic_baseline_manifest": args.semantic_baseline_manifest,
                "public_baseline_manifest": args.public_baseline_manifest,
                "source_attestation": args.source_attestation,
                "release_result": args.release_result,
                "release_metrics": args.release_metrics,
                "shadow_public_result": args.shadow_public_result,
                "semantic_diff": args.semantic_diff,
                "promoter_dry_run": args.promoter_dry_run,
                "build_context": args.build_context,
            }
            result = freeze_activation_bundle(
                paths,
                args.output_store,
                ci_evidence=fresh_ci,
                expected_semantic_baseline_id=expected_semantic_baseline_id,
                git=args.git,
            )
        elif args.command == "verify":
            result = verify_activation_bundle(args.bundle_root)
            if args.expected_bundle_id is not None:
                expected, _ = _require_release_id(
                    args.expected_bundle_id, "expected bundle ID"
                )
                if result.bundle_id != expected:
                    raise BundleValidationError(
                        f"bundle ID {result.bundle_id} differs from expected {expected}"
                    )
            if (
                args.expected_semantic_baseline_id is not None
                and result.semantic_baseline_release_id
                != _require_release_id(
                    args.expected_semantic_baseline_id,
                    "expected semantic baseline ID",
                )[0]
            ):
                raise BundleValidationError(
                    "bundle semantic baseline differs from the expected reviewed ID"
                )
        else:
            context = create_build_context(
                args.build_worktree, args.promotion_worktree, git=args.git
            )
            sys.stdout.buffer.write(_canonical_json_bytes(context))
            return 0
    except (OSError, ActivationBundleError, ActivationCIError) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, sort_keys=True), file=sys.stderr)
        return 1
    print(json.dumps(result.summary(), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
